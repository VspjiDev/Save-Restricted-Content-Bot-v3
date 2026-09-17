#!/usr/bin/env python3
"""Uploads run in parallel; only the message order is serial.

Downloads already ran WORKERS at a time, but every upload waited its turn in the
ordered stage, so a batch of small files spent almost all its time uploading one
at a time. Keeping posts in source order only requires the *message creation* to
be ordered - the bytes can go up whenever. Files are uploaded during the parallel
stage now and the ordered stage just attaches the waiting handle.
"""
import asyncio
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

from pyrogram.enums import MessageMediaType     # noqa: E402
import plugins.batch as B                       # noqa: E402
import utils.turbo as T                         # noqa: E402

B.PROGRESS_INTERVAL = 0.02
FAILS = []


def check(label, got, want=True):
    ok = got == want
    if not ok:
        FAILS.append(label)
    print(('  ok   ' if ok else '  FAIL ') + f'{label}: {got!r}' + ('' if ok else f' (want {want!r})'))


class Msg:
    def __init__(self, mid):
        self.id = mid
        self.text = self.caption = None
        self.media = MessageMediaType.DOCUMENT
        self.has_protected_content = True
        self.chat = type('C', (), {'id': -1})()
        self.empty = False
        for attr in ('video', 'audio', 'photo', 'voice', 'video_note',
                     'animation', 'sticker'):
            setattr(self, attr, None)
        self.document = type('D', (), {
            'file_name': f'doc_{mid}.pdf', 'file_size': 3 * 1024 * 1024, 'file_id': 'X'})()


class Src:
    async def get_messages(self, chat, ids):
        return [MESSAGES[i] for i in ids if i in MESSAGES]

    async def download_media(self, message, file_name=None, progress=None):
        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        await asyncio.sleep(0.02)
        open(file_name, 'wb').write(b'x' * 1024)
        return file_name


class Bot:
    """Records upload concurrency separately from message-send order."""
    def __init__(self):
        self.sent = []
        self.status = []
        self.uploading = 0
        self.peak_uploads = 0
        self.sending = 0
        self.peak_sends = 0

    async def edit_message_text(self, chat, mid, text):
        self.status.append(text)

    async def save_file(self, path, progress=None, **kw):
        self.uploading += 1
        self.peak_uploads = max(self.peak_uploads, self.uploading)
        try:
            await asyncio.sleep(0.08)        # uploads are the slow part
            return f'HANDLE:{os.path.basename(path)}'
        finally:
            self.uploading -= 1

    async def send_document(self, dest, path, **kw):
        self.sending += 1
        self.peak_sends = max(self.peak_sends, self.sending)
        try:
            await asyncio.sleep(0.01)
            self.sent.append(os.path.basename(path))
        finally:
            self.sending -= 1


MESSAGES = {}


async def settings(uid):
    return {'chat_id': None, 'caption': '', 'rename_tag': '',
            'replacement_words': {}, 'delete_words': []}


async def main():
    global MESSAGES
    MESSAGES = {100 + i: Msg(100 + i) for i in range(10)}
    src, bot = Src(), Bot()

    async def pick(*a, **k):
        return (src, -1, False)

    B.get_forward_settings = settings
    B.pick_source = pick
    B.TURBO_DISABLED = True
    B.WORKERS = 4
    B.BATCH_DELAY = 0
    B.ACTIVE[9] = {'cancel': False}
    await B.run_batch(bot, src, '9', 'private', 100, 10, 9, 5,
                      type('S', (), {'id': 1})())
    B.ACTIVE.pop(9, None)

    print('uploads overlap, sends do not')
    check('all 10 delivered', len(bot.sent), 10)
    check('several files uploaded at once', bot.peak_uploads > 1, True)
    check('upload concurrency capped at WORKERS', bot.peak_uploads <= 4, True)
    check('messages created one at a time', bot.peak_sends, 1)
    check('source order preserved', bot.sent, [f'doc_{100+i}.pdf' for i in range(10)])

    print('\nthe handle is reused, not uploaded twice')
    check('no stale handles left', len(T._uploaded), 0)

    shutil.rmtree('downloads', ignore_errors=True)
    print('\n' + ('ALL PASS' if not FAILS else f'FAILURES: {FAILS}'))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
