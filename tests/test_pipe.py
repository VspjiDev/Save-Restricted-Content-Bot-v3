#!/usr/bin/env python3
"""The pipelined transfer must be byte-exact and actually overlap.

Telegram throttles per account, so past a point more streams stop helping - but
it throttles the two directions separately. Downloading a file completely and
only then uploading it means the wall clock is download + upload; running them
together is close to half that for the same per-direction speed.

This checks the bytes come out right, that every upload part is sent exactly
once with a consistent total, and that the two directions really were in flight
at the same time rather than one after the other.
"""
import asyncio
import hashlib
import os
import sys
import tempfile
import time

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
LATENCY = 0.02


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
    """Records when each direction was busy, so overlap can be measured."""
    def __init__(self, data, log):
        self.data = data
        self.log = log

    async def invoke(self, query, sleep_threshold=None):
        if isinstance(query, raw.functions.upload.GetFile):
            self.log['down_busy'] += 1
            self.log['peak_down'] = max(self.log['peak_down'], self.log['down_busy'])
            self.log['overlap'] = max(self.log['overlap'],
                                      min(self.log['down_busy'], self.log['up_busy']))
            try:
                await asyncio.sleep(LATENCY)
                return raw.types.upload.File(
                    type=raw.types.storage.FileUnknown(), mtime=0,
                    bytes=self.data[query.offset:query.offset + query.limit])
            finally:
                self.log['down_busy'] -= 1

        if isinstance(query, raw.functions.upload.SaveBigFilePart):
            self.log['up_busy'] += 1
            self.log['overlap'] = max(self.log['overlap'],
                                      min(self.log['down_busy'], self.log['up_busy']))
            try:
                await asyncio.sleep(LATENCY)
                if query.file_part in self.log['parts']:
                    self.log['duplicates'] += 1
                self.log['parts'][query.file_part] = query.bytes
                self.log['totals'].add(query.file_total_parts)
                self.log['ids'].add(query.file_id)
                return True
            finally:
                self.log['up_busy'] -= 1

        raise AssertionError(f'unexpected {query}')


class Storage:
    async def dc_id(self): return 2


class Client:
    def __init__(self): self.storage = Storage()
    def rnd_id(self): return 987654321


def make_message(size):
    msg = type('M', (), {})()
    for attr in ('document', 'audio', 'photo', 'voice', 'video_note', 'animation', 'sticker'):
        setattr(msg, attr, None)
    msg.video = type('V', (), {'file_size': size, 'file_id': 'X'})()
    return msg


async def main():
    T.FileId = type('F', (), {'decode': staticmethod(lambda s: type('I', (), {
        'file_type': 999, 'dc_id': 2, 'media_id': 1, 'access_hash': 2,
        'file_reference': b'', 'thumb_size': '', 'thumbnail_size': ''})())})

    size = 30 * 1024 * 1024 + 7777        # deliberately ragged
    data = body(size)
    log = {'parts': {}, 'totals': set(), 'ids': set(), 'duplicates': 0,
           'down_busy': 0, 'up_busy': 0, 'peak_down': 0, 'overlap': 0}
    sessions = [Session(data, log) for _ in range(8)]

    async def pool(client, dc_id, streams):
        return sessions

    T.get_pool = pool
    tmp = tempfile.mkdtemp()
    dest = os.path.join(tmp, 'movie.mp4')

    down_seen, up_seen = [], []
    started = time.time()
    result = await T.pipe_transfer(
        Client(), Client(), make_message(size), dest, 8,
        lambda c, t: down_seen.append(c), lambda c, t: up_seen.append(c))
    elapsed = time.time() - started

    print('correctness')
    check('returned the path', result, dest)
    check('file on disk is byte-identical', open(dest, 'rb').read() == data)
    expected_parts = (size + T.UPLOAD_PART - 1) // T.UPLOAD_PART
    check('every upload part sent', sorted(log['parts']), list(range(expected_parts)))
    check('no part sent twice', log['duplicates'], 0)
    check('one consistent total_parts', log['totals'], {expected_parts})
    check('one file id for the whole upload', len(log['ids']), 1)
    check('uploaded bytes match the source',
          b''.join(log['parts'][i] for i in range(expected_parts)) == data)

    print('\nhandle handed to save_file')
    handle = T.take_upload(dest)
    check('InputFileBig registered', isinstance(handle, raw.types.InputFileBig))
    check('part count matches', handle.parts, expected_parts)
    check('consumed only once', T.take_upload(dest), None)

    print('\nthe two directions overlapped')
    check('download and upload were in flight together', log['overlap'] >= 1, True)
    check('download used several streams', log['peak_down'] > 1, True)
    # Strictly sequential would be at least (down parts + up parts) * latency.
    sequential = (len(log['parts']) + (size + T.DOWNLOAD_PART - 1) // T.DOWNLOAD_PART) * LATENCY
    print(f'    took {elapsed:.2f}s; one-at-a-time would be >= {sequential:.2f}s')
    check('faster than doing them one after the other', elapsed < sequential, True)

    print('\nprogress reported for both directions')
    check('download progress ends at size', down_seen[-1], size)
    check('upload progress ends at size', up_seen[-1], size)

    print('\nrename keeps the uploaded handle')
    T.register_upload(dest, raw.types.InputFileBig(id=1, parts=2, name='movie.mp4'))
    renamed = os.path.join(tmp, 'movie tag.mp4')
    T.rekey_upload(dest, renamed)
    moved = T.take_upload(renamed)
    check('handle followed the new name', moved is not None, True)
    check('name updated', moved.name if moved else None, 'movie tag.mp4')

    print('\n' + ('ALL PASS' if not FAILS else f'FAILURES: {FAILS}'))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
