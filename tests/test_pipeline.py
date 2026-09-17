"""Exercise run_batch against fake clients: ordering, parallelism, cancel, cleanup."""
import asyncio
import os
import shutil
import sys

sys.path.insert(0, '/home/user/Save-Restricted-Content-Bot-v3')
os.chdir('/home/user/Save-Restricted-Content-Bot-v3')

import plugins.batch as B

B.PROGRESS_INTERVAL = 0.02
B.BATCH_DELAY = 0
FAILS = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILS.append(label)
    print(('  ok   ' if ok else '  FAIL ') + f'{label}: {got!r}' + ('' if ok else f' (want {want!r})'))


class Msg:
    def __init__(self, mid, kind='video', protected=True, text=None):
        self.id = mid
        self.text = None
        self.caption = None
        self.media = kind != 'text'
        self.has_protected_content = protected
        self.chat = type('C', (), {'id': -100999})()
        self.empty = False
        for attr in ('video', 'audio', 'document', 'photo', 'voice', 'video_note', 'sticker'):
            setattr(self, attr, None)
        if kind == 'video':
            self.video = type('V', (), {'file_name': f'clip_{mid}.mp4'})()
        elif kind == 'text':
            self.text = type('T', (), {'markdown': f'text {mid}'})()


class FakeSource:
    def __init__(self, messages, delay=0.05):
        self.messages = messages
        self.delay = delay
        self.live = 0
        self.peak = 0
        self.fetch_calls = 0

    async def get_chat(self, x):
        return type('C', (), {'id': -100999})()

    async def get_dialogs(self, limit=100):
        if False:
            yield None

    async def get_messages(self, chat, ids):
        self.fetch_calls += 1
        return [self.messages[i] for i in ids if i in self.messages]

    async def download_media(self, message, file_name=None, progress=None):
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            os.makedirs(os.path.dirname(file_name), exist_ok=True)
            await asyncio.sleep(self.delay)
            with open(file_name, 'wb') as fh:
                fh.write(b'0' * 2048)
            if progress:
                progress(1024, 2048)
                progress(2048, 2048)
            return file_name
        finally:
            self.live -= 1


class FakeBot:
    def __init__(self):
        self.sent = []
        self.status = []
        self.live = 0
        self.peak = 0

    async def edit_message_text(self, chat, mid, text, reply_markup=None, **kw):
        self.status.append(text)

    async def send_message(self, dest, text, **kw):
        self.sent.append(('text', text))

    async def _upload(self, tag, path, progress=None):
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            await asyncio.sleep(0.02)
            if progress:
                progress(2048, 2048)
            self.sent.append((tag, os.path.basename(path)))
        finally:
            self.live -= 1

    async def send_video(self, dest, path, progress=None, **kw):
        await self._upload('video', path, progress)

    async def send_document(self, dest, path, progress=None, **kw):
        await self._upload('document', path, progress)

    async def send_photo(self, dest, path, progress=None, **kw):
        await self._upload('photo', path, progress)

    async def send_audio(self, dest, path, progress=None, **kw):
        await self._upload('audio', path, progress)

    async def copy_message(self, dest, from_chat, mid, **kw):
        self.sent.append(('copy', mid))


SETTINGS = {'chat_id': None, 'caption': '', 'rename_tag': '',
            'replacement_words': {}, 'delete_words': []}


async def fake_settings(uid):
    return dict(SETTINGS)


B.get_forward_settings = fake_settings


async def run(messages, workers=4, via_bot=False, cancel_after=None, delay=0.05):
    B.WORKERS = workers
    src = FakeSource(messages, delay)
    bot = FakeBot()

    async def pick(*a, **k):
        return (src, -100999, via_bot)

    B.pick_source = pick
    uid = 4242
    B.ACTIVE[uid] = {'cancel': False}

    ids = sorted(messages)
    status = type('S', (), {'id': 7})()

    if cancel_after is not None:
        async def canceller():
            while bot.sent.__len__() < cancel_after:
                await asyncio.sleep(0.01)
            B.ACTIVE[uid]['cancel'] = True
        asyncio.create_task(canceller())

    await B.run_batch(bot, src, '999', 'private', ids[0], len(ids), uid, 555, status)
    B.ACTIVE.pop(uid, None)
    return src, bot


async def main():
    print('1. ordered uploads while downloads run in parallel')
    msgs = {100 + i: Msg(100 + i) for i in range(12)}
    src, bot = await run(msgs, workers=4)
    names = [n for tag, n in bot.sent if tag == 'video']
    check('all 12 uploaded', len(names), 12)
    check('upload order preserved', names, [f'clip_{100+i}.mp4' for i in range(12)])
    check('downloads ran in parallel', src.peak > 1, True)
    check('download parallelism capped at WORKERS', src.peak <= 4, True)
    check('uploads stayed serial', bot.peak, 1)
    check('bulk fetch used one call', src.fetch_calls, 1)
    check('summary says completed', '✅' in bot.status[-1], True)
    check('temp dir cleaned', os.path.isdir(os.path.join('downloads', '4242')), False)

    print('\n2. bulk fetch chunking (250 posts -> 3 calls)')
    msgs = {i: Msg(i, kind='text') for i in range(1, 251)}
    src, bot = await run(msgs, workers=4, delay=0)
    check('chunked into 100s', src.fetch_calls, 3)
    check('all texts sent', len([1 for t, _ in bot.sent if t == 'text']), 250)

    print('\n3. server-side copy for unprotected posts (zero transfer)')
    msgs = {10 + i: Msg(10 + i, protected=False) for i in range(6)}
    src, bot = await run(msgs, workers=3, via_bot=True)
    check('copied not downloaded', [t for t, _ in bot.sent], ['copy'] * 6)
    check('copy order preserved', [m for _, m in bot.sent], list(range(10, 16)))

    print('\n4. cancellation stops early and cleans up')
    msgs = {200 + i: Msg(200 + i) for i in range(30)}
    src, bot = await run(msgs, workers=4, cancel_after=5)
    check('stopped before finishing', len(bot.sent) < 30, True)
    check('summary says cancelled', '🛑' in bot.status[-1], True)
    check('temp files removed', os.path.isdir(os.path.join('downloads', '4242')), False)

    print('\n5. a failing download is counted, not fatal')
    msgs = {300 + i: Msg(300 + i) for i in range(6)}
    src = FakeSource(msgs, 0.01)
    original = src.download_media

    async def flaky(message, file_name=None, progress=None):
        if message.id == 302:
            raise RuntimeError('simulated network drop')
        return await original(message, file_name=file_name, progress=progress)

    src.download_media = flaky
    bot = FakeBot()

    async def pick(*a, **k):
        return (src, -100999, False)

    B.pick_source = pick
    B.WORKERS = 3
    B.ACTIVE[4242] = {'cancel': False}
    await B.run_batch(bot, src, '999', 'private', 300, 6, 4242, 555, type('S', (), {'id': 7})())
    B.ACTIVE.pop(4242, None)
    check('5 of 6 survived', len([1 for t, _ in bot.sent if t == 'video']), 5)
    check('failure reported', '**Failed**: 1' in bot.status[-1], True)
    check('good posts still in order', [n for t, n in bot.sent if t == 'video'],
          [f'clip_{i}.mp4' for i in (300, 301, 303, 304, 305)])

    shutil.rmtree('downloads', ignore_errors=True)
    print('\n' + ('ALL PASS' if not FAILS else f'FAILURES: {FAILS}'))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
