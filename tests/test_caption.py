#!/usr/bin/env python3
"""Media must never degrade into a caption-only message.

Two bugs this exists for, both of which showed up as "the PDF/video did not
arrive, only the caption did":

1. prepare_message decided "this has no file" from whether it recognised the
   message attribute. Any downloadable type outside that list - paid media, a
   story, anything a newer pyrogram adds - became a caption-only text message.
   It is driven by the media-type enum now, and anything not explicitly
   text-only still gets a download attempt.

2. Telegram rejects a media caption over 1024 characters outright with
   MEDIA_CAPTION_TOO_LONG; it does not truncate. build_caption pasted the
   original caption and the user's custom caption together with no limit, so a
   long post lost its file entirely. Plain messages allow 4096, which is why
   text-only posts kept working and only media went missing.
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

B.PROGRESS_INTERVAL = 0.02
FAILS = []


def check(label, got, want=True):
    ok = got == want
    if not ok:
        FAILS.append(label)
    print(('  ok   ' if ok else '  FAIL ') + f'{label}: {got!r}' + ('' if ok else f' (want {want!r})'))


class Cap:
    def __init__(self, text):
        self.markdown = text


class Msg:
    def __init__(self, mid, media_type, caption=None, attr=None):
        self.id = mid
        self.text = None
        self.caption = Cap(caption) if caption else None
        self.media = media_type
        self.has_protected_content = True
        self.chat = type('C', (), {'id': -1})()
        self.empty = False
        for name in ('video', 'document', 'audio', 'photo', 'voice',
                     'video_note', 'animation', 'sticker'):
            setattr(self, name, None)
        if attr:
            setattr(self, attr, type('M', (), {
                'file_name': f'file_{mid}.pdf', 'file_size': 1024, 'file_id': 'X'})())


class Src:
    def __init__(self):
        self.downloads = []

    async def get_messages(self, chat, ids):
        return [MESSAGES[i] for i in ids if i in MESSAGES]

    async def download_media(self, message, file_name=None, progress=None):
        self.downloads.append(message.id)
        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        open(file_name, 'wb').write(b'x' * 64)
        return file_name


class Bot:
    def __init__(self):
        self.sent = []
        self.status = []

    async def edit_message_text(self, chat, mid, text):
        self.status.append(text)

    async def send_message(self, dest, text, **kw):
        self.sent.append(('text', text))

    async def send_document(self, dest, path, caption=None, progress=None, **kw):
        self.sent.append(('document', caption))


SETTINGS = {'chat_id': None, 'caption': '', 'rename_tag': '',
            'replacement_words': {}, 'delete_words': []}
MESSAGES = {}


async def run(messages, custom_caption=''):
    global MESSAGES
    MESSAGES = messages
    src, bot = Src(), Bot()

    async def settings(uid):
        return dict(SETTINGS, caption=custom_caption)

    async def pick(*a, **k):
        return (src, -1, False)

    B.get_forward_settings = settings
    B.pick_source = pick
    B.TURBO_DISABLED = True          # exercise the plain path
    B.WORKERS = 2
    B.BATCH_DELAY = 0
    B.ACTIVE[3] = {'cancel': False}
    ids = sorted(messages)
    await B.run_batch(bot, src, '9', 'private', ids[0], len(ids), 3, 5,
                      type('S', (), {'id': 1})())
    B.ACTIVE.pop(3, None)
    return src, bot


async def main():
    print('an unrecognised media type still gets downloaded')
    # PAID_MEDIA is downloadable but has no attribute in turbo's lookup list
    exotic = MessageMediaType.PAID_MEDIA
    msgs = {10: Msg(10, exotic, caption='Polity Compilation Class 11 & 12')}
    src, bot = await run(msgs)
    check('download was attempted', src.downloads, [10])
    check('sent as a file, not a caption', [k for k, _ in bot.sent], ['document'])
    check('caption did not arrive on its own', ('text', 'Polity Compilation Class 11 & 12')
          not in bot.sent, True)

    print('\ngenuinely text-only media is still sent as text')
    msgs = {20: Msg(20, MessageMediaType.WEB_PAGE_PREVIEW, caption='just a link')}
    src, bot = await run(msgs)
    check('no download attempted', src.downloads, [])
    check('sent as text', [k for k, _ in bot.sent], ['text'])

    print('\na normal document is unaffected')
    msgs = {30: Msg(30, MessageMediaType.DOCUMENT, caption='notes', attr='document')}
    src, bot = await run(msgs)
    check('downloaded and sent', [k for k, _ in bot.sent], ['document'])

    print('\nan over-long caption keeps the file')
    long_caption = 'A' * 900
    msgs = {40: Msg(40, MessageMediaType.DOCUMENT, caption=long_caption, attr='document')}
    src, bot = await run(msgs, custom_caption='B' * 400)   # 900 + 2 + 400 = 1302
    kinds = [k for k, _ in bot.sent]
    docs = [c for k, c in bot.sent if k == 'document']
    texts = [c for k, c in bot.sent if k == 'text']
    check('file still sent', 'document' in kinds, True)
    caption_sent = docs[0] if docs else ''
    check('caption within Telegram limit', len(caption_sent or '') <= B.CAPTION_LIMIT, True)
    check('overflow followed as its own message', kinds.count('text'), 1)
    check('no text was lost',
          (caption_sent or '') + (texts[0] if texts else '') == long_caption + '\n\n' + 'B' * 400,
          True)

    print('\na short caption sends no extra message')
    msgs = {50: Msg(50, MessageMediaType.DOCUMENT, caption='short', attr='document')}
    src, bot = await run(msgs)
    check('one message only', len(bot.sent), 1)

    shutil.rmtree('downloads', ignore_errors=True)
    print('\n' + ('ALL PASS' if not FAILS else f'FAILURES: {FAILS}'))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
