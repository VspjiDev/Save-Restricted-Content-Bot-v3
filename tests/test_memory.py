#!/usr/bin/env python3
"""Memory stays inside its budget however many files are running.

Chunks sit in RAM between arriving and being sent. Without a ceiling that is
WORKERS x TURBO_STREAMS x chunk size, so raising either could push a small dyno
into swapping (Heroku R14) - which makes everything slower, the opposite of what
raising them was for. A global slot count bounds it instead, so WORKERS is safe
to turn up: transfers share the budget rather than each taking their own.

The other half of this is leaks: a slot that is taken and never given back
shrinks the budget permanently, and enough of them deadlock every transfer. So
every path out of a chunk - short final chunk, retries exhausted, CDN redirect,
empty chunk, a failure elsewhere - has to give its slots back.
"""
import asyncio
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for key, value in {
    'API_ID': '123', 'API_HASH': 'test', 'BOT_TOKEN': '1:test',
    'MONGO_DB': 'mongodb://127.0.0.1:27017', 'OWNER_ID': '1',
    'MASTER_KEY': 'test', 'IV_KEY': 'test',
}.items():
    os.environ.setdefault(key, value)

from pyrogram import raw          # noqa: E402
import utils.turbo as T           # noqa: E402

FAILS = []


def check(label, got, want=True):
    ok = got == want
    if not ok:
        FAILS.append(label)
    print(('  ok   ' if ok else '  FAIL ') + f'{label}: {got!r}' + ('' if ok else f' (want {want!r})'))


def body(size):
    out = bytearray()
    i = 0
    while len(out) < size:
        out += hashlib.sha256(str(i).encode()).digest()
        i += 1
    return bytes(out[:size])


class Session:
    def __init__(self, data, log, fail_mode=None):
        self.data = data
        self.log = log
        self.fail_mode = fail_mode

    async def invoke(self, query, sleep_threshold=None):
        if isinstance(query, raw.functions.upload.GetFile):
            if self.fail_mode == 'error':
                raise RuntimeError('link dropped')
            if self.fail_mode == 'cdn':
                return raw.types.upload.FileCdnRedirect(
                    dc_id=2, file_token=b'', encryption_key=b'', encryption_iv=b'',
                    file_hashes=[])
            held = T._MEMORY_SLOTS - T.memory_gate()._value
            self.log['peak_slots'] = max(self.log['peak_slots'], held)
            await asyncio.sleep(0.002)
            return raw.types.upload.File(
                type=raw.types.storage.FileUnknown(), mtime=0,
                bytes=self.data[query.offset:query.offset + query.limit])

        await asyncio.sleep(0.002)
        return True


class Storage:
    async def dc_id(self): return 2


class Client:
    def __init__(self): self.storage = Storage()
    def rnd_id(self): return 7


def message(size):
    m = type('M', (), {})()
    for attr in ('document', 'audio', 'photo', 'voice', 'video_note',
                 'animation', 'sticker'):
        setattr(m, attr, None)
    m.video = type('V', (), {'file_size': size, 'file_id': 'X'})()
    return m


async def main():
    T.FileId = type('F', (), {'decode': staticmethod(lambda s: type('I', (), {
        'file_type': 999, 'dc_id': 2, 'media_id': 1, 'access_hash': 2,
        'file_reference': b'', 'thumb_size': '', 'thumbnail_size': ''})())})

    # a deliberately tiny budget, so a breach is unmistakable
    T._MEMORY_SLOTS = 8
    T._memory = None
    free = T._MEMORY_SLOTS
    tmp = tempfile.mkdtemp()

    size = 11 * 1024 * 1024 + 1234          # ragged: the last chunk is short
    data = body(size)
    log = {'peak_slots': 0}

    print('ten files at once stay inside the budget')
    sessions = [Session(data, log) for _ in range(8)]

    async def pool(c, d, s): return sessions
    T.get_pool = pool

    results = await asyncio.gather(*(
        T.pipe_transfer(Client(), Client(), message(size),
                        os.path.join(tmp, f'f{i}.bin'), 8)
        for i in range(10)))
    check('all ten completed', all(r is not None for r in results), True)
    check('never exceeded the budget', log['peak_slots'] <= T._MEMORY_SLOTS, True)
    check('budget fully returned', T.memory_gate()._value, free)
    check('bytes correct', open(os.path.join(tmp, 'f3.bin'), 'rb').read() == data, True)

    print('\nfailures give their slots back too')
    for label, mode in (('network error', 'error'), ('CDN redirect', 'cdn')):
        broken = [Session(data, log, fail_mode=mode) for _ in range(4)]

        async def bad_pool(c, d, s, _b=broken): return _b
        T.get_pool = bad_pool
        out = await T.pipe_transfer(Client(), Client(), message(size),
                                    os.path.join(tmp, 'bad.bin'), 4)
        check(f'{label}: declined', out, None)
        check(f'{label}: budget intact', T.memory_gate()._value, free)

    print('\nthe plain download path returns its slots as well')
    T.get_pool = pool
    out = await T.turbo_download(Client(), message(size), os.path.join(tmp, 'p.bin'), 8)
    check('downloaded', out is not None, True)
    check('budget intact', T.memory_gate()._value, free)
    check('bytes correct', open(os.path.join(tmp, 'p.bin'), 'rb').read() == data, True)

    broken = [Session(data, log, fail_mode='error') for _ in range(4)]

    async def bad_pool(c, d, s): return broken
    T.get_pool = bad_pool
    check('failed download declines', await T.turbo_download(
        Client(), message(size), os.path.join(tmp, 'q.bin'), 4), None)
    check('budget intact after failure', T.memory_gate()._value, free)

    print('\n' + ('ALL PASS' if not FAILS else f'FAILURES: {FAILS}'))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
