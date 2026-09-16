#!/usr/bin/env python3
"""A broken turbo must never cost a post.

The bug this exists for: turbo_download built its connection pool *outside* its
try/except. When the pool failed, the exception escaped turbo_download and
prepare_message, the post was marked failed, and the media simply never arrived
- while pyrogram, which would have delivered it, was never even tried. Uploads
had the guard and fell back correctly, which is why they only looked slow.

Files under 5MB skipped turbo entirely, so small ones kept working and the
failure looked random.
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

import plugins.batch as B          # noqa: E402
import utils.turbo as T            # noqa: E402

B.PROGRESS_INTERVAL = 0.02
FAILS = []


def check(label, got, want=True):
    ok = got == want
    if not ok:
        FAILS.append(label)
    print(('  ok   ' if ok else '  FAIL ') + f'{label}: {got!r}' + ('' if ok else f' (want {want!r})'))


class Msg:
    def __init__(self, mid, size=20 * 1024 * 1024, kind='document'):
        self.id = mid
        self.text = self.caption = None
        self.media = kind is not None
        self.has_protected_content = True
        self.chat = type('C', (), {'id': -100999})()
        self.empty = False
        for attr in ('video', 'document', 'audio', 'photo', 'voice',
                     'video_note', 'animation', 'sticker'):
            setattr(self, attr, None)
        if kind == 'document':
            self.document = type('D', (), {
                'file_name': f'notes_{mid}.pdf', 'file_size': size, 'file_id': 'FAKE',
            })()
        elif kind == 'webpage':
            # a link preview: message.media is set but nothing is downloadable
            self.text = type('T', (), {'markdown': f'preview text {mid}'})()


class Src:
    """pyrogram's downloader - always works, stands in for the fallback."""
    def __init__(self):
        self.calls = 0

    async def get_messages(self, chat, ids):
        return [MESSAGES[i] for i in ids if i in MESSAGES]

    async def download_media(self, message, file_name=None, progress=None):
        self.calls += 1
        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        with open(file_name, 'wb') as fh:
            fh.write(b'x' * 4096)
        if progress:
            progress(4096, 4096)
        return file_name


class Bot:
    def __init__(self):
        self.sent = []
        self.status = []

    async def edit_message_text(self, chat, mid, text):
        self.status.append(text)

    async def send_message(self, dest, text, **kw):
        self.sent.append(('text', text))

    async def send_document(self, dest, path, progress=None, **kw):
        if progress:
            progress(4096, 4096)
        self.sent.append(('document', os.path.basename(path)))

    async def send_video(self, dest, path, progress=None, **kw):
        self.sent.append(('video', os.path.basename(path)))


async def settings(uid):
    return {'chat_id': None, 'caption': '', 'rename_tag': '',
            'replacement_words': {}, 'delete_words': []}


B.get_forward_settings = settings
MESSAGES = {}


async def run(messages, pool_error=None):
    global MESSAGES
    MESSAGES = messages
    src, bot = Src(), Bot()

    async def pick(*a, **k):
        return (src, -100999, False)

    async def pool(client, dc_id, size):
        if pool_error:
            raise pool_error
        return []

    B.pick_source = pick
    T.get_pool = pool
    T.FileId = type('F', (), {'decode': staticmethod(lambda s: type('I', (), {
        'file_type': 999, 'dc_id': 2, 'media_id': 1, 'access_hash': 2,
        'file_reference': b'', 'thumb_size': '', 'thumbnail_size': ''})())})
    B.WORKERS = 2
    B.BATCH_DELAY = 0
    B.ACTIVE[7] = {'cancel': False}
    ids = sorted(messages)
    await B.run_batch(bot, src, '9', 'private', ids[0], len(ids), 7, 5,
                      type('S', (), {'id': 1})())
    B.ACTIVE.pop(7, None)
    return src, bot


async def main():
    print('a failing turbo pool must not cost the media')
    msgs = {10 + i: Msg(10 + i) for i in range(4)}
    src, bot = await run(msgs, pool_error=ConnectionError('pool build failed'))
    docs = [n for kind, n in bot.sent if kind == 'document']
    check('all 4 media still delivered', len(docs), 4)
    check('via pyrogram fallback', src.calls, 4)
    check('reported as sent, not failed', '4/4 sent' in bot.status[-1], True)
    check('turbo failure was recorded', bool(T.STATE['last_error']), True)

    print('\nan exception mid-download also falls back')
    T.STATE['last_error'] = None
    msgs = {20 + i: Msg(20 + i) for i in range(3)}
    src, bot = await run(msgs, pool_error=RuntimeError('auth import rejected'))
    check('all 3 delivered', len([1 for k, _ in bot.sent if k == 'document']), 3)

    print('\nlink previews are sent as text, not failed')
    msgs = {30 + i: Msg(30 + i, kind='webpage') for i in range(3)}
    src, bot = await run(msgs)
    check('sent as text', len([1 for k, _ in bot.sent if k == 'text']), 3)
    check('nothing marked failed', '❌ 0 failed' in bot.status[-1], True)

    print('\na genuine failure explains itself in the summary')
    msgs = {40: Msg(40)}

    async def dead(message, file_name=None, progress=None):
        raise OSError('no space left on device')

    MESSAGES.update(msgs)
    src, bot = Src(), Bot()
    src.download_media = dead

    async def pick(*a, **k):
        return (src, -100999, False)

    B.pick_source = pick
    B.ACTIVE[7] = {'cancel': False}
    await B.run_batch(bot, src, '9', 'private', 40, 1, 7, 5, type('S', (), {'id': 1})())
    B.ACTIVE.pop(7, None)
    check('failure counted', '❌ 1 failed' in bot.status[-1], True)
    check('reason shown to the user', 'no space left on device' in bot.status[-1], True)

    shutil.rmtree('downloads', ignore_errors=True)
    print('\n' + ('ALL PASS' if not FAILS else f'FAILURES: {FAILS}'))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
