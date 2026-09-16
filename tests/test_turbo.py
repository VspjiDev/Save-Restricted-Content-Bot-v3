"""Exercise the parallel transfer engine against mocked MTProto sessions."""
import asyncio
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, '/home/user/Save-Restricted-Content-Bot-v3')
os.chdir('/home/user/Save-Restricted-Content-Bot-v3')

from pyrogram import raw
import utils.turbo as T

FAILS = []


def check(label, got, want=True):
    ok = got == want
    if not ok:
        FAILS.append(label)
    print(('  ok   ' if ok else '  FAIL ') + f'{label}: {got!r}' + ('' if ok else f' (want {want!r})'))


def body(size):
    """Deterministic pseudo-content so we can verify exact reassembly."""
    out = bytearray()
    i = 0
    while len(out) < size:
        out += hashlib.sha256(str(i).encode()).digest()
        i += 1
    return bytes(out[:size])


class FakeFileId:
    file_type = 999
    dc_id = 2
    media_id = 1
    access_hash = 2
    file_reference = b''
    thumb_size = ''
    thumbnail_size = ''


class FakeSession:
    """Records every request so we can assert on protocol constraints."""
    def __init__(self, data, log, fail_on=None):
        self.data = data
        self.log = log
        self.fail_on = fail_on
        self.concurrent = 0

    async def invoke(self, query, sleep_threshold=None):
        self.log['live'] += 1
        self.log['peak'] = max(self.log['peak'], self.log['live'])
        try:
            await asyncio.sleep(0.005)
            if isinstance(query, raw.functions.upload.GetFile):
                self.log['gets'].append((query.offset, query.limit))
                if self.fail_on is not None and query.offset == self.fail_on:
                    raise RuntimeError('simulated chunk failure')
                return raw.types.upload.File(
                    type=raw.types.storage.FileUnknown(),
                    mtime=0,
                    bytes=self.data[query.offset:query.offset + query.limit],
                )
            if isinstance(query, raw.functions.upload.SaveBigFilePart):
                self.log['parts'][query.file_part] = query.bytes
                self.log['totals'].add(query.file_total_parts)
                if self.fail_on is not None and query.file_part == self.fail_on:
                    raise RuntimeError('simulated part failure')
                return True
            raise AssertionError(f'unexpected query {query}')
        finally:
            self.log['live'] -= 1


class FakeStorage:
    async def dc_id(self): return 2
    async def test_mode(self): return False
    async def auth_key(self): return b'k' * 256


class FakeClient:
    def __init__(self): self.storage = FakeStorage()
    def rnd_id(self): return 123456789
    async def invoke(self, q): raise AssertionError('should not reach the network')


def make_message(size):
    media = type('M', (), {'file_size': size, 'file_id': 'FAKE'})()
    msg = type('Msg', (), {})()
    for attr in ('document', 'audio', 'photo', 'voice', 'video_note', 'animation', 'sticker'):
        setattr(msg, attr, None)
    msg.video = media
    return msg


async def main():
    T.FileId = type('F', (), {'decode': staticmethod(lambda s: FakeFileId())})
    tmp = tempfile.mkdtemp()

    # ── download: byte exact reassembly with a ragged final chunk ──────────────
    print('download')
    size = 10 * 1024 * 1024 + 12345          # not a multiple of the chunk size
    data = body(size)
    log = {'gets': [], 'live': 0, 'peak': 0, 'parts': {}, 'totals': set()}
    sessions = [FakeSession(data, log) for _ in range(8)]

    async def pool(client, dc_id, streams): return sessions
    T.get_pool = pool

    dest = os.path.join(tmp, 'out.bin')
    progress = []
    result = await T.turbo_download(FakeClient(), make_message(size), dest, 8,
                                    lambda c, t: progress.append((c, t)))
    check('returned the path', result, dest)
    check('file size exact', os.path.getsize(dest), size)
    check('content byte-identical', open(dest, 'rb').read() == data)
    check('every part requested once', sorted(o for o, _ in log['gets']),
          [i * T.DOWNLOAD_PART for i in range(11)])
    check('offsets are 1MB aligned', all(o % T.DOWNLOAD_PART == 0 for o, _ in log['gets']))
    check('limit divides 1MB and is 4096-aligned',
          all(l == T.DOWNLOAD_PART and l % 4096 == 0 for _, l in log['gets']))
    check('chunks were in flight together', log['peak'] > 1)
    check('progress ends at file size', progress[-1], (size, size))
    check('progress never exceeds total', all(c <= t for c, t in progress))

    # ── download: small file is left to pyrogram ──────────────────────────────
    small = await T.turbo_download(FakeClient(), make_message(1024), os.path.join(tmp, 's.bin'), 8)
    check('small file declines turbo', small, None)

    # ── download: failure falls back and leaves no partial file ───────────────
    log2 = {'gets': [], 'live': 0, 'peak': 0, 'parts': {}, 'totals': set()}
    broken = [FakeSession(data, log2, fail_on=3 * T.DOWNLOAD_PART) for _ in range(4)]

    async def pool2(client, dc_id, streams): return broken
    T.get_pool = pool2
    dest2 = os.path.join(tmp, 'broken.bin')
    check('failure returns None', await T.turbo_download(FakeClient(), make_message(size), dest2, 4), None)
    check('no partial file left behind', os.path.exists(dest2), False)

    # ── upload: every part sent exactly once, correct bytes ───────────────────
    print('\nupload')
    up_size = 20 * 1024 * 1024 + 999
    up_data = body(up_size)
    src = os.path.join(tmp, 'in.bin')
    open(src, 'wb').write(up_data)

    log3 = {'gets': [], 'live': 0, 'peak': 0, 'parts': {}, 'totals': set()}
    up_sessions = [FakeSession(b'', log3) for _ in range(8)]

    async def pool3(client, dc_id, streams): return up_sessions
    T.get_pool = pool3

    prog = []
    handle = await T.turbo_upload(FakeClient(), src, 8, lambda c, t: prog.append((c, t)))
    expected_parts = (up_size + T.UPLOAD_PART - 1) // T.UPLOAD_PART
    check('returned InputFileBig', isinstance(handle, raw.types.InputFileBig))
    check('part count correct', handle.parts, expected_parts)
    check('name preserved', handle.name, 'in.bin')
    check('all parts uploaded', sorted(log3['parts']), list(range(expected_parts)))
    check('reassembled upload matches source',
          b''.join(log3['parts'][i] for i in range(expected_parts)) == up_data)
    check('one consistent total_parts', log3['totals'], {expected_parts})
    check('part size divides 524288', 524288 % T.UPLOAD_PART, 0)
    check('parts were in flight together', log3['peak'] > 1)
    check('upload progress ends at size', prog[-1], (up_size, up_size))

    check('small upload declines turbo',
          await T.turbo_upload(FakeClient(), os.path.join(tmp, 's2.bin'), 8)
          if open(os.path.join(tmp, 's2.bin'), 'wb').write(b'x' * 1000) else None, None)

    # ── install(): patches save_file and falls back correctly ─────────────────
    print('\ninstall()')
    calls = {'original': 0}

    class C(FakeClient):
        async def save_file(self, path, file_id=None, file_part=0, progress=None, progress_args=()):
            calls['original'] += 1
            return 'ORIGINAL'

    client = C()
    client.save_file = client.save_file          # bind like pyrogram does
    T.install(client, 8)
    check('is idempotent', (T.install(client, 8), client._turbo_installed)[1], True)

    T.get_pool = pool3
    out = await client.save_file(src, progress=None)
    check('big file used turbo', isinstance(out, raw.types.InputFileBig))
    check('original untouched for big file', calls['original'], 0)

    tiny = os.path.join(tmp, 'tiny.bin')
    open(tiny, 'wb').write(b'x' * 500)
    check('small file used pyrogram', await client.save_file(tiny), 'ORIGINAL')

    check('resumed part used pyrogram', await client.save_file(src, file_id=5, file_part=2), 'ORIGINAL')

    print('\n' + ('ALL PASS' if not FAILS else f'FAILURES: {FAILS}'))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
