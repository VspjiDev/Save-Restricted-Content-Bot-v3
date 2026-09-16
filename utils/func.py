# Copyright (c) 2025 Vsp Official
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

import asyncio
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorClient

from config import MONGO_DB as MONGO_URI, DB_NAME

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "mpeg", "mpg", "3gp", "m4v", "ogv"}
AUDIO_EXTENSIONS = {"mp3", "wav", "flac", "aac", "ogg", "wma", "m4a", "opus", "aiff", "ac3"}

mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[DB_NAME]
users_collection = db["users"]

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

# ── link parsing ────────────────────────────────────────────────────────────────

# https://t.me/c/1234567/89  |  https://t.me/c/1234567/12/89  (topic)
PRIVATE_LINK = re.compile(r'(?:https?://)?(?:t\.me|telegram\.me)/c/(\d+)(?:/\d+)?/(\d+)')
# https://t.me/channelname/89  |  https://t.me/channelname/12/89  (topic)
PUBLIC_LINK = re.compile(r'(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][\w_]{3,})(?:/\d+)?/(\d+)')
# https://t.me/b/botname/89
BOT_LINK = re.compile(r'(?:https?://)?(?:t\.me|telegram\.me)/b/([\w_]+)/(\d+)')


def E(link):
    """Parse a telegram post link -> (chat, message_id, link_type)."""
    link = (link or '').strip()

    m = PRIVATE_LINK.match(link)
    if m:
        return f'-100{m.group(1)}', int(m.group(2)), 'private'

    m = BOT_LINK.match(link)
    if m:
        return m.group(1), int(m.group(2)), 'public'

    m = PUBLIC_LINK.match(link)
    if m:
        return m.group(1), int(m.group(2)), 'public'

    return None, None, None


def is_private_link(link):
    return bool(PRIVATE_LINK.match(link or ''))


def sanitize(filename):
    """Strip characters that break filesystems, keep it short enough for ext4."""
    cleaned = re.sub(r'[<>:"/\\|?*\'\n\r\t]', '_', str(filename)).strip(' .')
    return cleaned[:200] or f'file_{int(time.time())}'


sanitize_filename = sanitize


def thumbnail(user_id):
    path = f'{user_id}.jpg'
    return path if os.path.exists(path) else None


def hhmmss(seconds):
    return time.strftime('%H:%M:%S', time.gmtime(seconds))


def human_bytes(size):
    size = float(size or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.2f} {unit}'
        size /= 1024


def get_display_name(user):
    if not user:
        return 'Unknown User'
    first = getattr(user, 'first_name', None)
    last = getattr(user, 'last_name', None)
    if first and last:
        return f'{first} {last}'
    return first or last or getattr(user, 'username', None) or 'Unknown User'


async def is_private_chat(event):
    return event.is_private


# ── user settings (cached) ──────────────────────────────────────────────────────
#
# A batch used to hit MongoDB 4+ times per message (chat_id, caption, rename tag,
# replacement/delete words). That is pure latency on the hot path, so user
# documents are cached in-process and invalidated on every write.

_USER_CACHE = {}
_USER_CACHE_TTL = 300


def invalidate_user_cache(user_id=None):
    if user_id is None:
        _USER_CACHE.clear()
    else:
        _USER_CACHE.pop(int(user_id), None)


async def get_user_data(user_id, cached=True):
    user_id = int(user_id)
    if cached:
        hit = _USER_CACHE.get(user_id)
        if hit and time.time() - hit[0] < _USER_CACHE_TTL:
            return hit[1]
    try:
        data = await users_collection.find_one({"user_id": user_id})
    except Exception as e:
        logger.error(f'Error retrieving user data for {user_id}: {e}')
        return None
    _USER_CACHE[user_id] = (time.time(), data)
    return data


async def get_user_data_key(user_id, key, default=None):
    data = await get_user_data(user_id)
    return data.get(key, default) if data else default


async def save_user_data(user_id, key, value):
    await users_collection.update_one(
        {"user_id": int(user_id)},
        {"$set": {key: value}},
        upsert=True,
    )
    invalidate_user_cache(user_id)


async def save_user_session(user_id, session_string):
    try:
        await users_collection.update_one(
            {"user_id": int(user_id)},
            {"$set": {"session_string": session_string, "updated_at": datetime.now()}},
            upsert=True,
        )
        invalidate_user_cache(user_id)
        logger.info(f'Saved session for user {user_id}')
        return True
    except Exception as e:
        logger.error(f'Error saving session for user {user_id}: {e}')
        return False


async def remove_user_session(user_id):
    try:
        await users_collection.update_one({"user_id": int(user_id)}, {"$unset": {"session_string": ""}})
        invalidate_user_cache(user_id)
        logger.info(f'Removed session for user {user_id}')
        return True
    except Exception as e:
        logger.error(f'Error removing session for user {user_id}: {e}')
        return False


async def get_forward_settings(user_id):
    """Every per-message setting in one cached document read."""
    data = await get_user_data(user_id) or {}
    return {
        'chat_id': data.get('chat_id'),
        'caption': data.get('caption', ''),
        'rename_tag': data.get('rename_tag', ''),
        'replacement_words': data.get('replacement_words', {}) or {},
        'delete_words': data.get('delete_words', []) or [],
    }


def apply_text_rules(text, replacements=None, delete_words=None):
    """Synchronous caption/filename rewriting - no DB hit, safe on the hot path."""
    if not text:
        return ''
    for word, replacement in (replacements or {}).items():
        text = text.replace(word, replacement)
    if delete_words:
        text = ' '.join(w for w in text.split() if w not in delete_words)
    return text


async def process_text_with_rules(user_id, text):
    if not text:
        return ''
    try:
        settings = await get_forward_settings(user_id)
        return apply_text_rules(text, settings['replacement_words'], settings['delete_words'])
    except Exception as e:
        logger.error(f'Error processing text with rules: {e}')
        return text


# ── media helpers (ffmpeg/ffprobe based, no opencv) ──────────────────────────────

async def _run(cmd, timeout=60):
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 1, b'', b'binary not found'
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return 1, b'', b'timeout'
    return proc.returncode, out, err


async def get_video_metadata(file_path):
    """Width/height/duration via ffprobe.

    ffprobe only reads the container header, while the old opencv path opened and
    decoded the file - on a multi-GB video that alone cost several seconds.
    """
    default = {'width': 1, 'height': 1, 'duration': 1}
    if not FFPROBE or not file_path or not os.path.exists(file_path):
        return default

    code, out, _ = await _run([
        FFPROBE, '-v', 'quiet', '-print_format', 'json',
        '-show_entries', 'stream=width,height,duration:format=duration',
        '-select_streams', 'v:0', file_path,
    ], timeout=30)
    if code != 0:
        return default

    try:
        info = json.loads(out.decode() or '{}')
        stream = (info.get('streams') or [{}])[0]
        duration = stream.get('duration') or info.get('format', {}).get('duration') or 0
        return {
            'width': int(stream.get('width') or 1),
            'height': int(stream.get('height') or 1),
            'duration': max(int(float(duration)), 1),
        }
    except Exception as e:
        logger.error(f'ffprobe parse error: {e}')
        return default


async def screenshot(video, duration, sender):
    """Grab a thumbnail. A user supplied thumb always wins."""
    custom = f'{sender}.jpg'
    if os.path.exists(custom):
        return custom
    if not FFMPEG or not video or not os.path.exists(video):
        return None

    output = os.path.join(
        os.path.dirname(video) or '.',
        f'thumb_{sender}_{int(time.time() * 1000)}.jpg',
    )
    # -ss before -i seeks by keyframe without decoding the leading frames, and the
    # 320px scale keeps the thumb inside Telegram's limits.
    code, _, err = await _run([
        FFMPEG, '-ss', hhmmss(max(int(duration or 0) // 2, 0)), '-i', video,
        '-frames:v', '1', '-vf', 'scale=320:-2', '-q:v', '5', '-y', output,
    ], timeout=60)

    if os.path.isfile(output):
        return output
    logger.warning(f'ffmpeg thumbnail failed: {err.decode(errors="ignore").strip()[:200]}')
    return None
