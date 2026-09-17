#!/usr/bin/env python3
"""Several posts move at once, sharing one connection pool.

Each pipelined transfer uses every session in the pool, so with WORKERS files
in flight the same sessions carry several files' chunks at the same time. This
checks that actually works: the bytes stay correct per file, uploads overlap
across files, and the messages are still created in source order.
"""
import asyncio
import hashlib
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for key, value in {
    'API_ID': '123', 'API_HASH': 'test', 'BOT_TOKEN': '1:test',
    'MONGO_DB': 'mongodb://127.0.0.1:27017', 'OWNER_ID': '1',
    'MASTER_KEY': 'test', 'IV_KEY': 'test',
}.items():
    os.environ.setdefault(key, value)

from pyrogram import raw                          # noqa: E402
from pyrogram.enums import MessageMediaType       # noqa: E402
import plugins.batch as B                         # noqa: E402
import utils.turbo as T                           # noqa: E402

B.PROGRESS_INTERVAL = 0.02
FAILS = []
SIZE = 12 * 1024 * 1024 + 4321        # above the pipe threshold, ragged


def check(label, got, want=True):
    ok = got == want
    if not ok:
        FAILS.append(label)
    print(('  ok   ' if ok else '  FAIL ') + f'{label}: {got!r}' + ('' if ok else f' (want {want!r})'))


def body(mid, size):
    """Distinct content per post, so a mix-up between files is visible."""
    out = bytearray()
    i = 0
    while len(out) < size:
        out += hashlib.sha256(f'{mid}:{i}'.encode()).digest()
        i += 1
    return bytes(out[:size])


CONTENT = {}


class Msg:
    def __init__(self, mid):
        self.id = mid
        self.text = self.caption = None
        self.media = MessageMediaType.VIDEO
        self.has_protected_content = True
        self.chat = type('C', (), {'id': -1})()
        self.empty = False
        for attr in ('document', 'audio', 'photo', 'voice', 'video_note',
                     'animation', 'sticker'):
            setattr(self, attr, None)
        self.video = type('V', (), {
            'file_name': f'clip_{mid}.mp4', 'file_size': SIZE, 'file_id': f'FILE{mid}',
            'duration': 10, 'width': 1280, 'height': 720, 'thumbs': None})()


class Session:
    """One shared session, used by every file in flight at once."""
    def __init__(self, log):
        self.log = log

    async def invoke(self, query, sleep_threshold=None):
        if isinstance(query, raw.functions.upload.GetFile):
            mid = query.location.id
            await asyncio.sleep(0.005)
            return raw.types.upload.File(
                type=raw.types.storage.FileUnknown(), mtime=0,
                bytes=CONTENT[mid][query.offset:query.offset + query.limit])

        if isinstance(query, raw.functions.upload.SaveBigFilePart):
            self.log['up_live'] += 1
            self.log['peak_up'] = max(self.log['peak_up'], self.log['up_live'])
            self.log['files_uploading'].add(query.file_id)
            self.log['peak_files'] = max(self.log['peak_files'],
                                         len(self.log['files_uploading']))
            try:
                await asyncio.sleep(0.005)
                self.log['parts'].setdefault(query.file_id, {})[query.file_part] = query.bytes
                return True
            finally:
                self.log['up_live'] -= 1
                if self.log['parts'].get(query.file_id, {}) and \
                        len(self.log['parts'][query.file_id]) * T.UPLOAD_PART >= SIZE:
                    self.log['files_uploading'].discard(query.file_id)


class Storage:
    async def dc_id(self): return 2


class Src:
    def __init__(self): self.storage = Storage()
    def rnd_id(self): return 0
    async def get_messages(self, chat, ids):
        return [MESSAGES[i] for i in ids if i in MESSAGES]


class Bot:
    def __init__(self):
        self.storage = Storage()
        self.sent = []
        self.status = []
        self.sending = 0
        self.peak_sends = 0
        self._id = 1000

    def rnd_id(self):
        self._id += 1
        return self._id

    async def edit_message_text(self, chat, mid, text, reply_markup=None, **kw):
        self.status.append(text)

    async def send_video(self, dest, path, **kw):
        self.sending += 1
        self.peak_sends = max(self.peak_sends, self.sending)
        try:
            await asyncio.sleep(0.01)
            self.sent.append(os.path.basename(path))
        finally:
            self.sending -= 1

    async def save_file(self, path, progress=None, **kw):
        handle = T.take_upload(path)
        if handle is None:
            raise AssertionError(f'{path} was not pre-uploaded')
        return handle


MESSAGES = {}


async def settings(uid):
    return {'chat_id': None, 'caption': '', 'rename_tag': '',
            'replacement_words': {}, 'delete_words': []}


async def main():
    global MESSAGES
    ids = list(range(200, 206))
    MESSAGES = {i: Msg(i) for i in ids}
    for i in ids:
        CONTENT[i] = body(i, SIZE)

    log = {'parts': {}, 'up_live': 0, 'peak_up': 0,
           'files_uploading': set(), 'peak_files': 0}
    sessions = [Session(log) for _ in range(8)]

    async def pool(client, dc_id, streams):
        return sessions

    T.get_pool = pool
    T.FileId = type('F', (), {'decode': staticmethod(lambda s: type('I', (), {
        'file_type': 999, 'dc_id': 2, 'media_id': int(s.replace('FILE', '')),
        'access_hash': 2, 'file_reference': b'', 'thumb_size': '',
        'thumbnail_size': ''})())})

    src, bot = Src(), Bot()

    async def pick(*a, **k):
        return (src, -1, False)

    B.get_forward_settings = settings
    B.pick_source = pick
    B.TURBO_DISABLED = False
    B.TURBO_STREAMS = 8
    B.WORKERS = 4                     # what a user sets to run 3-4 at once
    B.BATCH_DELAY = 0
    B.ACTIVE[11] = {'cancel': False}
    await B.run_batch(bot, src, '9', 'private', ids[0], len(ids), 11, 5,
                      type('S', (), {'id': 1})())
    B.ACTIVE.pop(11, None)

    print('every post arrived, in order')
    check('all delivered', len(bot.sent), len(ids))
    check('source order kept', bot.sent, [f'clip_{i}.mp4' for i in ids])
    check('messages created one at a time', bot.peak_sends, 1)

    print('\nfiles really did overlap')
    check('several files uploading at once', log['peak_files'] > 1, True)
    check('within the WORKERS cap', log['peak_files'] <= 4, True)
    check('many parts in flight', log['peak_up'] > 1, True)

    print('\nno file got another file\'s bytes')
    expected = (SIZE + T.UPLOAD_PART - 1) // T.UPLOAD_PART
    check('one upload per post', len(log['parts']), len(ids))
    sizes = {fid: len(parts) for fid, parts in log['parts'].items()}
    check('each upload complete', set(sizes.values()), {expected})
    rebuilt = sorted(b''.join(p[i] for i in range(expected)) for p in log['parts'].values())
    check('content matches the sources', rebuilt == sorted(CONTENT[i] for i in ids), True)

    check('no handles left behind', len(T._uploaded), 0)

    shutil.rmtree('downloads', ignore_errors=True)
    print('\n' + ('ALL PASS' if not FAILS else f'FAILURES: {FAILS}'))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
