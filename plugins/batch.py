# Copyright (c) 2025 Vsp Official
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

"""Restricted content forwarder.

Speed design, in short:

* a public post that is not protected is copied server side, so zero bytes
  are transferred at all
* everything else is downloaded and re-uploaded through utils/turbo.py, which
  uses many connections per file instead of pyrogram's single chunk at a time
* one bulk ``get_messages`` per 100 posts instead of one call per post
* chat/peer resolution and dialog refreshes are cached instead of repeated
* downloads run ``WORKERS`` at a time while uploads stay strictly in order
* one throttled status message for the whole run instead of an edit per file
"""

import asyncio
import os
import shutil
import time
from typing import Any, Dict, Optional

from pyrogram import filters
from pyrogram.enums import MessageMediaType
from pyrogram.errors import FloodWait, MessageNotModified

from config import (
    BATCH_DELAY,
    BRAND,
    BATCH_LIMIT,
    DOWNLOAD_DIR,
    LOG_GROUP,
    PROGRESS_INTERVAL,
    TURBO_DISABLED,
    TURBO_STREAMS,
    WORKERS,
)
from plugins.settings import rename_file_sync
from plugins.start import subscribe as sub
from shared_client import app as X, build_client, turbocharge, userbot as Y
from utils import turbo
from utils.custom_filters import login_in_progress, settings_in_progress
from utils.encrypt import dcs
from utils.func import (
    AUDIO_EXTENSIONS,
    E,
    VIDEO_EXTENSIONS,
    apply_text_rules,
    get_forward_settings,
    get_user_data,
    get_video_metadata,
    human_bytes,
    sanitize,
    screenshot,
    thumbnail,
)

# uid -> the user's logged in account, the only client that can read
# restricted posts. Uploads always go out through the bot itself.
UC: Dict[int, Any] = {}

Z: Dict[int, Dict[str, Any]] = {}       # uid -> conversation state
ACTIVE: Dict[int, Dict[str, Any]] = {}  # uid -> running batch

TWO_GB = 2000 * 1024 * 1024

# Telegram caps a media caption at 1024 characters but allows 4096 in a plain
# message. Going over does not truncate, it rejects the whole send with
# MEDIA_CAPTION_TOO_LONG - which loses the file, not just the extra words.
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096

# Media types with no file behind them. Built defensively: a pyrogram version
# that does not know one of these simply leaves it out.
TEXT_ONLY_MEDIA = {
    getattr(MessageMediaType, name, None) for name in (
        'WEB_PAGE_PREVIEW', 'POLL', 'CONTACT', 'LOCATION', 'VENUE',
        'DICE', 'GAME', 'GIVEAWAY', 'GIVEAWAY_RESULT', 'INVOICE', 'TODO', 'STORY',
    )
} - {None}


def is_user_active(uid: int) -> bool:
    return uid in ACTIVE


def should_cancel(uid: int) -> bool:
    return ACTIVE.get(uid, {}).get('cancel', False)


# ── clients ─────────────────────────────────────────────────────────────────────

async def get_uclient(uid: int):
    """The user's own account - the only client that can read restricted posts."""
    if uid in UC:
        return UC[uid]

    data = await get_user_data(uid) or {}
    stored = data.get('session_string')
    if stored:
        try:
            # /login stores an encrypted string, /settings stores a raw one.
            try:
                session = dcs(stored)
            except Exception:
                session = stored
            client = build_client(
                f'{uid}_client',
                session_string=session,
                device_model=BRAND,
                no_updates=True,          # this client never handles updates
            )
            await client.start()
            turbocharge(client)
            await ensure_dialogs(client, force=True)
            UC[uid] = client
            return client
        except Exception as e:
            print(f'User client error for {uid}: {e}')

    return Y


# ── peer resolution (cached) ────────────────────────────────────────────────────

_dialog_refresh: Dict[int, float] = {}
_peer_cache: Dict[tuple, int] = {}
DIALOG_TTL = 600


async def ensure_dialogs(client, force: bool = False, ttl: int = DIALOG_TTL):
    """Warm the peer cache.

    The old code walked up to 200 dialogs for *every single message*; now it
    happens at most once every ten minutes per client.
    """
    key = id(client)
    if not force and time.time() - _dialog_refresh.get(key, 0) < ttl:
        return
    _dialog_refresh[key] = time.time()
    try:
        async for _ in client.get_dialogs(limit=100):
            pass
    except Exception as e:
        print(f'Failed to refresh dialogs: {e}')


async def resolve_chat(client, chat, link_type: str) -> Optional[int]:
    """Resolve a link's chat to a real peer id, once per client per chat."""
    key = (id(client), str(chat))
    if key in _peer_cache:
        return _peer_cache[key]

    raw = str(chat)
    if link_type == 'private':
        digits = raw.lstrip('-')
        digits = digits[3:] if digits.startswith('100') else digits
        candidates = [int(f'-100{digits}'), int(f'-{digits}')]
    else:
        candidates = [raw, f'@{raw.lstrip("@")}']

    for attempt, candidate in enumerate(candidates):
        try:
            resolved = await client.get_chat(candidate)
            _peer_cache[key] = resolved.id
            return resolved.id
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception:
            if attempt == 0:
                # a private peer the session has not seen yet -> one dialog sweep
                await ensure_dialogs(client, force=True)
    return None


async def fetch_messages(client, chat, ids):
    """Bulk fetch: one API call per 100 posts instead of one per post."""
    found = {}
    for start in range(0, len(ids), 100):
        chunk = ids[start:start + 100]
        for _ in range(2):
            try:
                messages = await client.get_messages(chat, chunk)
                break
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except Exception as e:
                print(f'Bulk fetch error: {e}')
                messages = []
                break
        else:
            messages = []

        if messages and not isinstance(messages, list):
            messages = [messages]
        for message in messages or []:
            if message and not getattr(message, 'empty', False) and message.id is not None:
                found[message.id] = message
    return found


async def pick_source(bot, user_client, chat, link_type, probe_id):
    """Decide who reads the source chat.

    If the bot itself can read a public chat we can copy posts by file id and
    skip the transfer entirely, which is by far the fastest path.
    """
    if link_type == 'public' and bot:
        chat_id = await resolve_chat(bot, chat, link_type)
        if chat_id:
            try:
                probe = await bot.get_messages(chat_id, probe_id)
                if probe and not getattr(probe, 'empty', False):
                    return bot, chat_id, True
            except Exception:
                pass

    if not user_client:
        return None, None, False

    if link_type == 'public':
        try:
            await user_client.join_chat(str(chat).lstrip('@'))
        except Exception:
            pass

    chat_id = await resolve_chat(user_client, chat, link_type)
    if chat_id is None:
        return None, None, False
    return user_client, chat_id, False


# ── live progress ───────────────────────────────────────────────────────────────

class Tracker:
    """One throttled status message for the whole run.

    Progress callbacks are plain functions that only touch counters, so a
    transfer is never blocked waiting on a Telegram edit.
    """

    def __init__(self, client, chat_id, message_id, total):
        self.client = client
        self.chat_id = chat_id
        self.message_id = message_id
        self.total = total
        self.done = self.ok = self.fail = 0
        self.active = 0          # posts being transferred right now
        self.probe = None        # how many are prepared and waiting to be sent
        self.downloaded = self.uploaded = 0
        self.start = time.time()
        self.note = 'starting'
        self._last_text = ''
        self._stop = False

    def callback(self, kind: str):
        seen = {'v': 0}

        def progress(current, total):
            delta = current - seen['v']
            seen['v'] = current
            if delta < 0:
                delta = current
            if kind == 'down':
                self.downloaded += delta
            else:
                self.uploaded += delta

        return progress

    def render(self):
        elapsed = max(time.time() - self.start, 0.001)
        filled = int((self.done / self.total) * 10) if self.total else 0
        bar = '▰' * filled + '▱' * (10 - filled)
        percent = (self.done / self.total * 100) if self.total else 0

        eta = '--:--'
        if self.done and self.done < self.total:
            eta = duration((elapsed / self.done) * (self.total - self.done))

        # Separate rates, because one combined number hides which direction is
        # actually the slow one.
        down_rate = human_bytes(self.downloaded / elapsed)
        up_rate = human_bytes(self.uploaded / elapsed)

        waiting = self.probe() if self.probe else 0
        return (
            '🚀 **Rocket Forward**\n\n'
            f'`[{bar}]` {percent:.1f}%\n'
            f'📦 **Posts**: {self.done}/{self.total}  ·  ✅ {self.ok}  ·  ❌ {self.fail}\n'
            f'⚡ **Transferring**: {self.active} of {WORKERS}  ·  '
            f'**waiting to send**: {waiting}\n'
            f'⬇️ {human_bytes(self.downloaded)} ({down_rate}/s)\n'
            f'⬆️ {human_bytes(self.uploaded)} ({up_rate}/s)\n'
            f'⏱ **Elapsed**: {duration(elapsed)}  ·  **ETA**: {eta}\n'
            f'📄 {self.note}'
        )

    async def flush(self):
        text = self.render()
        if text == self._last_text:
            return
        self._last_text = text
        try:
            await self.client.edit_message_text(self.chat_id, self.message_id, text)
        except (MessageNotModified, FloodWait):
            pass
        except Exception:
            pass

    async def run(self):
        try:
            while not self._stop:
                await asyncio.sleep(PROGRESS_INTERVAL)
                await self.flush()
        except asyncio.CancelledError:
            pass

    def stop(self):
        self._stop = True


async def with_flood(factory, retries: int = 3):
    """Await a coroutine factory, sitting out FloodWaits instead of failing."""
    last = None
    for _ in range(retries):
        try:
            return await factory()
        except FloodWait as e:
            last = e
            await asyncio.sleep(e.value + 1)
    if last:
        raise last


# ── per message work ────────────────────────────────────────────────────────────

def duration(seconds):
    """h:mm:ss, because %M:%S silently drops the hours.

    A long batch was reporting an ETA of minutes when the real answer was half a
    day, and an elapsed time that reset to 00:00 every hour.
    """
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes:02d}:{secs:02d}'


def build_caption(message, settings):
    """Returns (caption, overflow).

    Anything past Telegram's media-caption limit is handed back separately so it
    can follow as its own message. Silently dropping it would lose text; leaving
    it in would make Telegram reject the file.
    """
    original = message.caption.markdown if message.caption else ''
    processed = apply_text_rules(original, settings['replacement_words'], settings['delete_words'])
    custom = settings['caption'] or ''
    if processed and custom:
        full = f'{processed}\n\n{custom}'
    else:
        full = custom or processed

    if not full:
        return None, None
    if len(full) <= CAPTION_LIMIT:
        return full, None
    return full[:CAPTION_LIMIT], full[CAPTION_LIMIT:][:TEXT_LIMIT]


def media_filename(message, uid):
    media = message.video or message.audio or message.document
    name = getattr(media, 'file_name', None) if media else None
    if not name:
        if message.video or message.video_note:
            name = f'video_{message.id}.mp4'
        elif message.audio:
            name = f'audio_{message.id}.mp3'
        elif message.photo:
            name = f'photo_{message.id}.jpg'
        elif message.voice:
            name = f'voice_{message.id}.ogg'
        else:
            name = f'file_{message.id}.bin'
    # Each post gets its own directory: parallel downloads of two posts that
    # share a file name (a channel full of "video.mp4") must not collide, and
    # the name Telegram shows stays clean.
    return os.path.join(os.path.abspath(DOWNLOAD_DIR), str(uid), str(message.id), sanitize(name))


async def prepare_message(source, message, uid, settings, tracker, via_bot,
                          user_copy_ok=False, bot=None):
    """Download stage. Returns a plan the ordered upload stage can execute."""
    caption, overflow = build_caption(message, settings)

    if not message.media:
        if message.text:
            return {'kind': 'text', 'message': message, 'text': message.text.markdown}
        return {'kind': 'skip'}

    if message.sticker:
        return {'kind': 'sticker', 'message': message}

    # A link preview, poll, contact or location counts as "media" but has no file
    # behind it, so send the words instead of attempting an impossible download.
    #
    # This is decided from the media type, never from whether we recognise the
    # attribute: treating "I don't know this one" as "it has no file" is how a
    # real PDF turns into a caption-only message. Anything not listed here gets
    # a download attempt, and pyrogram handles more types than turbo does.
    if message.media in TEXT_ONLY_MEDIA:
        body = message.text or message.caption
        if body:
            return {'kind': 'text', 'message': message, 'text': body.markdown}
        return {'kind': 'skip'}

    # Fastest possible path: the bot can read the post and it is not protected,
    # so Telegram copies it server side and nothing is transferred.
    if via_bot and not message.has_protected_content:
        return {'kind': 'copy', 'message': message, 'caption': caption, 'overflow': overflow}

    # Same trick through the user's own account. A private channel is not
    # necessarily a protected one - most are simply private - and the logged in
    # account can copy those straight across. Telegram moves them server side,
    # so no download or upload happens at all and per-account throttling is
    # irrelevant. Only used when posting into a configured channel, where a post
    # from the user's account reads as a channel post; in a DM it would arrive
    # from the wrong sender.
    if user_copy_ok and not message.has_protected_content:
        return {'kind': 'usercopy', 'message': message, 'caption': caption,
                'overflow': overflow, 'source': source}

    target = media_filename(message, uid)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    on_progress = tracker.callback('down')

    path = None
    if not TURBO_DISABLED:
        # Best case: download and upload run together, so the wall clock is one
        # transfer instead of two. The file comes back already uploaded and the
        # ordered send stage just attaches it.
        path = await turbo.pipe_transfer(
            source, bot, message, target, TURBO_STREAMS,
            on_progress, tracker.callback('up'),
        )

        if not path:
            # Many connections at once; returns None when it cannot help, in
            # which case we simply use pyrogram's downloader below.
            path = await turbo.turbo_download(
                source, message, target, TURBO_STREAMS, on_progress
            )

    if not path:
        # Never let a media post fall through silently: if turbo declined, this
        # is the transfer that actually has to deliver it.
        path = await with_flood(lambda: source.download_media(
            message, file_name=target, progress=on_progress
        ))
    if not path:
        return {'kind': 'failed', 'reason': f'post {message.id}: download returned nothing'}

    if settings['rename_tag'] or settings['delete_words'] or settings['replacement_words']:
        renamed = rename_file_sync(
            path, settings['rename_tag'], settings['delete_words'], settings['replacement_words']
        )
        if renamed != path:
            # keep any pre-uploaded handle pointing at the file it belongs to
            turbo.rekey_upload(path, renamed)
            path = renamed

    thumb_path = None
    if message.video:
        thumb_path = await fetch_source_thumb(source, message, uid)

    # Send the bytes up while we are still in the parallel stage, so several
    # files upload at once. Only the message creation has to stay in order, and
    # that is all the ordered stage does once the handle is waiting for it.
    # pipe_transfer has already done this for the files it handled.
    pre_upload = getattr(bot, 'save_file', None)
    if pre_upload is not None and not turbo.has_upload(path):
        try:
            if os.path.getsize(path) <= TWO_GB:      # >2GB goes via the userbot
                handle = await pre_upload(path, progress=tracker.callback('up'))
                if handle is not None:
                    turbo.register_upload(path, handle)
        except Exception as e:
            # Not fatal: the ordered stage will simply upload it itself.
            print(f'Pre-upload failed for post {message.id}, will retry inline: {e}')

    return {'kind': 'file', 'message': message, 'caption': caption, 'path': path,
            'overflow': overflow, 'thumb_path': thumb_path}


async def fetch_source_thumb(source, message, uid):
    """Download the thumbnail Telegram already made for this video.

    A few KB, versus ffmpeg seeking and decoding a frame out of a multi-GB file.
    """
    video = message.video
    thumbs = getattr(video, 'thumbs', None) if video else None
    if not thumbs:
        return None
    try:
        best = max(thumbs, key=lambda t: getattr(t, 'file_size', 0) or 0)
        target = os.path.join(os.path.abspath(DOWNLOAD_DIR), str(uid),
                              str(message.id), 'thumb.jpg')
        return await source.download_media(best.file_id, file_name=target)
    except Exception:
        return None


async def send_overflow(bot, plan, dest, reply_to):
    """Caption text that did not fit follows the file as its own message."""
    overflow = plan.get('overflow')
    if not overflow:
        return
    try:
        await with_flood(lambda: bot.send_message(
            dest, overflow, reply_to_message_id=reply_to, disable_web_page_preview=True))
    except Exception as e:
        print(f'Could not send caption overflow: {e}')


async def upload_plan(bot, plan, uid, dest, reply_to, tracker):
    """Upload stage - runs one at a time so the destination keeps post order."""
    kind = plan['kind']
    if kind == 'skip':
        return False
    if kind == 'failed':
        return False

    message = plan['message']

    if kind == 'text':
        await with_flood(lambda: bot.send_message(
            dest, plan['text'], reply_to_message_id=reply_to, disable_web_page_preview=True
        ))
        return True

    if kind == 'sticker':
        await with_flood(lambda: bot.send_sticker(
            dest, message.sticker.file_id, reply_to_message_id=reply_to
        ))
        return True

    if kind == 'usercopy':
        try:
            await with_flood(lambda: plan['source'].copy_message(
                dest, message.chat.id, message.id,
                caption=plan['caption'], reply_to_message_id=reply_to,
            ))
            await send_overflow(bot, plan, dest, reply_to)
            return True
        except Exception as e:
            print(f'User-account copy failed, transferring instead: {e}')
            return False

    if kind == 'copy':
        try:
            await with_flood(lambda: bot.copy_message(
                dest, message.chat.id, message.id,
                caption=plan['caption'], reply_to_message_id=reply_to,
            ))
            await send_overflow(bot, plan, dest, reply_to)
            return True
        except Exception as e:
            print(f'Server side copy failed, falling back: {e}')
            return False

    path = plan['path']
    caption = plan['caption']
    thumb = None
    try:
        size = os.path.getsize(path)
        extension = os.path.splitext(path)[1].lower().lstrip('.')
        is_video = bool(message.video) or extension in VIDEO_EXTENSIONS
        is_audio = bool(message.audio) or extension in AUDIO_EXTENSIONS

        thumb = thumbnail(uid)
        width = height = duration = None
        if is_video:
            # The source message already carries duration and dimensions, so
            # ffprobe only has to run when Telegram did not send them. On a
            # multi-GB video that skips a real amount of work, and generating a
            # thumbnail with ffmpeg costs even more - reuse the source's own.
            src_video = message.video
            if src_video and getattr(src_video, 'duration', None):
                width = getattr(src_video, 'width', None) or 1
                height = getattr(src_video, 'height', None) or 1
                duration = src_video.duration
            else:
                meta = await get_video_metadata(path)
                width, height, duration = meta['width'], meta['height'], meta['duration']
            if not thumb:
                thumb = plan.get('thumb_path') or await screenshot(path, duration, uid)

        if size > TWO_GB:
            return await upload_oversized(
                bot, plan, uid, dest, reply_to, tracker,
                thumb, width, height, duration, is_video,
            )

        progress = tracker.callback('up')
        if message.video_note:
            await with_flood(lambda: bot.send_video_note(
                dest, path, progress=progress, reply_to_message_id=reply_to))
        elif message.voice:
            await with_flood(lambda: bot.send_voice(
                dest, path, caption=caption, progress=progress, reply_to_message_id=reply_to))
        elif is_video:
            await with_flood(lambda: bot.send_video(
                dest, path, caption=caption, thumb=thumb, width=width, height=height,
                duration=duration, supports_streaming=True,
                progress=progress, reply_to_message_id=reply_to))
        elif is_audio:
            await with_flood(lambda: bot.send_audio(
                dest, path, caption=caption, thumb=thumb,
                progress=progress, reply_to_message_id=reply_to))
        elif message.photo:
            await with_flood(lambda: bot.send_photo(
                dest, path, caption=caption, progress=progress, reply_to_message_id=reply_to))
        else:
            await with_flood(lambda: bot.send_document(
                dest, path, caption=caption, thumb=thumb, force_document=True,
                progress=progress, reply_to_message_id=reply_to))

        await send_overflow(bot, plan, dest, reply_to)
        return True
    finally:
        cleanup(path, thumb, uid)


async def upload_oversized(bot, plan, uid, dest, reply_to, tracker,
                           thumb, width, height, duration, is_video):
    """Files above the 2 GB bot limit go out through the premium userbot."""
    if not Y or not LOG_GROUP:
        raise RuntimeError('file is larger than 2GB, set STRING and LOG_GROUP to send it')

    path, caption = plan['path'], plan['caption']
    progress = tracker.callback('up')
    await ensure_dialogs(Y)

    if is_video:
        sent = await with_flood(lambda: Y.send_video(
            LOG_GROUP, path, caption=caption, thumb=thumb, width=width,
            height=height, duration=duration, supports_streaming=True, progress=progress))
    else:
        sent = await with_flood(lambda: Y.send_document(
            LOG_GROUP, path, caption=caption, thumb=thumb, force_document=True, progress=progress))

    await with_flood(lambda: bot.copy_message(dest, LOG_GROUP, sent.id, reply_to_message_id=reply_to))
    await send_overflow(bot, plan, dest, reply_to)
    return True


def cleanup(path, thumb=None, uid=None):
    if path:
        turbo.take_upload(path)          # drop any pre-uploaded handle
    for candidate in (path, thumb):
        if not candidate:
            continue
        # never delete the user's own saved thumbnail
        if uid is not None and candidate == f'{uid}.jpg':
            continue
        try:
            if os.path.exists(candidate):
                os.remove(candidate)
        except Exception:
            pass
    if path:
        try:
            os.rmdir(os.path.dirname(path))   # the per-post directory, if empty
        except OSError:
            pass


# ── batch runner ────────────────────────────────────────────────────────────────

def parse_destination(settings, fallback):
    configured = settings.get('chat_id')
    if not configured:
        return fallback, None
    try:
        if '/' in str(configured):
            chat, _, topic = str(configured).partition('/')
            return int(chat), int(topic)
        return int(configured), None
    except ValueError:
        return fallback, None


async def run_batch(bot, user_client, chat, link_type, start_id, count, uid, user_chat, status):
    settings = await get_forward_settings(uid)          # one cached read for the run
    dest, reply_to = parse_destination(settings, user_chat)

    # Copying through the user's account only makes sense when the destination is
    # a channel they post to; in a DM the message would come from them, not the bot.
    user_copy_ok = bool(settings.get('chat_id')) and user_client is not None

    source, source_chat, via_bot = await pick_source(bot, user_client, chat, link_type, start_id)
    if not source:
        await bot.edit_message_text(user_chat, status.id, '❌ Could not access that chat. Are you logged in and a member?')
        return

    ids = [start_id + offset for offset in range(count)]
    await bot.edit_message_text(user_chat, status.id, f'🔎 Fetching {count} post(s)...')
    messages = await fetch_messages(source, source_chat, ids)
    if not messages:
        await bot.edit_message_text(user_chat, status.id, '❌ No messages found at that link.')
        return

    tracker = Tracker(bot, user_chat, status.id, count)
    updater = asyncio.create_task(tracker.run())

    problems = []          # so a failed run can say what went wrong
    semaphore = asyncio.Semaphore(max(1, WORKERS))
    window = max(1, WORKERS) + 2
    tasks: Dict[int, asyncio.Task] = {}
    launched = 0

    async def prepare(index):
        message = messages.get(ids[index])
        if message is None:
            return {'kind': 'skip'}
        async with semaphore:
            if should_cancel(uid):
                return {'kind': 'skip'}
            tracker.active += 1
            try:
                return await prepare_message(source, message, uid, settings, tracker,
                                             via_bot, user_copy_ok, bot)
            except Exception as e:
                kind = getattr(message.media, 'name', message.media)
                return {'kind': 'failed',
                        'reason': f'post {message.id} ({kind}): {type(e).__name__}: {e}'}
            finally:
                tracker.active -= 1

    tracker.probe = lambda: sum(1 for task in tasks.values() if task.done())

    try:
        for index in range(count):
            if should_cancel(uid):
                break

            while launched < count and launched < index + window:
                tasks[launched] = asyncio.create_task(prepare(launched))
                launched += 1

            plan = await tasks.pop(index)
            tracker.note = f'post {ids[index]}'

            try:
                sent = await upload_plan(bot, plan, uid, dest, reply_to, tracker)
                if not sent and plan['kind'] in ('copy', 'usercopy'):
                    # protected after all - fall back to a real transfer
                    plan = await prepare_message(
                        source, plan['message'], uid, settings, tracker,
                        via_bot=False, bot=bot
                    )
                    sent = await upload_plan(bot, plan, uid, dest, reply_to, tracker)
            except Exception as e:
                sent = False
                cleanup(plan.get('thumb_path'), uid=uid)
                tracker.note = f'post {ids[index]}: {str(e)[:60]}'
                problems.append(f'post {ids[index]}: {type(e).__name__}: {e}')
                cleanup(plan.get('path'), uid=uid)

            tracker.done += 1
            if sent:
                tracker.ok += 1
            elif plan['kind'] != 'skip':
                tracker.fail += 1
                if plan.get('reason'):
                    problems.append(plan['reason'])

            if BATCH_DELAY:
                await asyncio.sleep(BATCH_DELAY)
    finally:
        # CancelledError is a BaseException, so gather() is the only safe way to
        # drain the in-flight downloads. Their half written files are removed by
        # the rmtree below.
        for task in tasks.values():
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        tasks.clear()

        tracker.stop()
        updater.cancel()

        elapsed = duration(time.time() - tracker.start)
        moved = human_bytes(tracker.downloaded + tracker.uploaded)
        summary = (
            ('🛑 **Cancelled**' if should_cancel(uid) else '✅ **Completed**') + '\n\n'
            f'📦 Posts: {tracker.ok}/{count} sent · ❌ {tracker.fail} failed\n'
            f'📊 Transferred: {moved}\n'
            f'⏱ Took: {elapsed}  ·  {WORKERS} at a time'
        )
        if problems:
            shown = '\n'.join(f'· `{p[:150]}`' for p in problems[:3])
            summary += f'\n\n**Why some failed**\n{shown}'
            if len(problems) > 3:
                summary += f'\n· …and {len(problems) - 3} more'
        if turbo.STATE.get('last_error') and not turbo.STATE.get('ready'):
            summary += f"\n\n⚠️ Turbo inactive: `{turbo.STATE['last_error'][:150]}`"

        try:
            await bot.edit_message_text(user_chat, status.id, summary)
        except Exception:
            try:
                await bot.send_message(user_chat, summary)
            except Exception:
                pass

        shutil.rmtree(os.path.join(os.path.abspath(DOWNLOAD_DIR), str(uid)), ignore_errors=True)


# ── commands ────────────────────────────────────────────────────────────────────

@X.on_message(filters.command(['batch', 'single']) & filters.private)
async def process_cmd(client, message):
    uid = message.from_user.id
    if await sub(client, message) == 1:
        return

    if is_user_active(uid):
        await message.reply_text('You already have a running task. Use /stop to cancel it.')
        return

    single = message.command[0] == 'single'
    Z[uid] = {'step': 'single' if single else 'link'}
    await message.reply_text(
        'Send me the post link.' if single else 'Send me the **start** link.'
    )


@X.on_message(
    filters.command(['cancel', 'stop']) & filters.private
    & ~login_in_progress & ~settings_in_progress
)
async def cancel_cmd(client, message):
    uid = message.from_user.id
    Z.pop(uid, None)
    if is_user_active(uid):
        ACTIVE[uid]['cancel'] = True
        await message.reply_text('🛑 Cancelling — the running transfer will finish first.')
    else:
        await message.reply_text('No active batch found.')


@X.on_message(
    filters.text & filters.private
    & ~login_in_progress & ~settings_in_progress
    & ~filters.command([
        'start', 'help', 'status', 'set', 'settings',
        'batch', 'single', 'cancel', 'stop',
        'login', 'logout',
    ])
)
async def text_handler(client, message):
    uid = message.from_user.id
    state = Z.get(uid)
    if not state:
        return

    step = state['step']

    if step in ('link', 'single'):
        chat, msg_id, link_type = E(message.text)
        if not chat or not msg_id:
            await message.reply_text('❌ Invalid link format. Example: `https://t.me/c/1234567890/12`')
            Z.pop(uid, None)
            return

        state.update({'chat': chat, 'id': msg_id, 'type': link_type})
        if step == 'link':
            state['step'] = 'count'
            await message.reply_text('How many posts should I grab?')
            return

        Z.pop(uid, None)
        await start_run(client, message, uid, chat, msg_id, link_type, 1)

    elif step == 'count':
        if not message.text.strip().isdigit():
            await message.reply_text('Please send a valid number.')
            return
        count = int(message.text.strip())
        if count < 1 or count > BATCH_LIMIT:
            await message.reply_text(f'Pick a number between 1 and {BATCH_LIMIT}.')
            return

        Z.pop(uid, None)
        await start_run(client, message, uid, state['chat'], state['id'], state['type'], count)


async def start_run(client, message, uid, chat, msg_id, link_type, count):
    if is_user_active(uid):
        await message.reply_text('You already have a running task. Use /stop first.')
        return

    status = await message.reply_text('⚙️ Warming up the engines...')

    bot = X                      # every upload goes out through this bot
    user_client = await get_uclient(uid)
    if not user_client and link_type == 'private':
        await status.edit_text('Private links need /login first.')
        return

    ACTIVE[uid] = {'cancel': False, 'total': count}
    try:
        await run_batch(bot, user_client, chat, link_type, msg_id, count, uid, message.chat.id, status)
    except Exception as e:
        try:
            await status.edit_text(f'❌ Error: {str(e)[:150]}')
        except Exception:
            pass
    finally:
        ACTIVE.pop(uid, None)
