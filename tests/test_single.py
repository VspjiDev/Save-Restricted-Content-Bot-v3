#!/usr/bin/env python3
"""Only one copy of the bot may go live.

Scaling the Heroku dyno count instead of setting WORKERS runs several copies on
one token. Every one of them receives the same updates, so each command is
answered once per copy and they throttle each other into FloodWait. The lock
picks one; the rest idle and take over only if it dies.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for key, value in {
    'API_ID': '123', 'API_HASH': 'test', 'BOT_TOKEN': '1:test',
    'MONGO_DB': 'mongodb://127.0.0.1:27017', 'OWNER_ID': '1',
    'MASTER_KEY': 'test', 'IV_KEY': 'test',
}.items():
    os.environ.setdefault(key, value)

from pymongo.errors import DuplicateKeyError, PyMongoError    # noqa: E402
import utils.single as S                                      # noqa: E402

FAILS = []


def check(label, got, want=True):
    ok = got == want
    if not ok:
        FAILS.append(label)
    print(('  ok   ' if ok else '  FAIL ') + f'{label}: {got!r}' + ('' if ok else f' (want {want!r})'))


class FakeLocks:
    """Mimics find_one_and_update(upsert=True) the way MongoDB behaves.

    The part that matters: when the document exists but the filter does not
    match it, the upsert tries to insert a duplicate _id and raises.
    """
    def __init__(self):
        self.doc = None
        self.broken = False

    def _matches(self, f):
        if self.doc is None:
            return False
        for clause in f.get('$or', []):
            if 'expires_at' in clause:
                if self.doc['expires_at'] < clause['expires_at']['$lt']:
                    return True
            if 'owner' in clause and self.doc['owner'] == clause['owner']:
                return True
        return False

    async def find_one_and_update(self, f, update, upsert=False, return_document=None):
        if self.broken:
            raise PyMongoError('connection lost')
        if self.doc is not None and not self._matches(f):
            raise DuplicateKeyError('_id already exists')
        self.doc = {'_id': 'bot', **update['$set']}
        return self.doc

    async def find_one(self, f):
        if self.broken:
            raise PyMongoError('connection lost')
        return self.doc

    async def delete_one(self, f):
        if self.doc and self.doc.get('owner') == f.get('owner'):
            self.doc = None


async def main():
    locks = FakeLocks()
    S.locks = locks

    print('the first copy goes live')
    first = 'dyno.1:aaaa'
    S.IDENTITY = first
    check('first instance acquires', await S._claim(), True)
    check('it owns the lease', locks.doc['owner'], first)

    print('\nthe others stand down')
    for name in ('dyno.2:bbbb', 'dyno.3:cccc', 'dyno.4:dddd'):
        S.IDENTITY = name
        check(f'{name} refused', await S._claim(), False)
    check('lease still held by the first', locks.doc['owner'], first)

    print('\nthe holder renews without losing it')
    S.IDENTITY = first
    before = locks.doc['expires_at']
    await asyncio.sleep(0.01)
    check('renewal succeeds', await S._claim(), True)
    check('lease extended', locks.doc['expires_at'] > before, True)
    check('still the same owner', locks.doc['owner'], first)

    print('\na dead holder is taken over once the lease expires')
    locks.doc['expires_at'] = datetime.utcnow() - timedelta(seconds=1)
    S.IDENTITY = 'dyno.2:bbbb'
    check('waiting copy takes over', await S._claim(), True)
    check('new owner recorded', locks.doc['owner'], 'dyno.2:bbbb')

    print('\nacquire() blocks while another copy is live, then proceeds')
    S.IDENTITY = 'dyno.5:eeee'
    task = asyncio.create_task(S.acquire(poll_seconds=0.05))
    await asyncio.sleep(0.15)
    check('still waiting', task.done(), False)
    locks.doc['expires_at'] = datetime.utcnow() - timedelta(seconds=1)
    await asyncio.wait_for(task, timeout=2)
    check('went live after the lease expired', locks.doc['owner'], 'dyno.5:eeee')

    print('\na database problem must not silence the only copy')
    locks.broken = True
    check('claims anyway', await S._claim(), True)
    locks.broken = False

    print('\nrelease hands the lock back')
    await S.release()
    check('lock cleared', locks.doc, None)
    S.IDENTITY = 'dyno.9:ffff'
    check('next copy can take it', await S._claim(), True)

    print('\n' + ('ALL PASS' if not FAILS else f'FAILURES: {FAILS}'))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
