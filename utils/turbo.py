# Copyright (c) 2025 Vsp Official
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

"""Parallel multi-connection transfers.

Pyrogram moves one 1 MB chunk at a time over a single connection, and builds a
fresh session (auth handshake included) for every file it downloads. That caps a
transfer at roughly one chunk per round trip, which is why stock speeds sit in
the low single-digit MB/s no matter how fast the server is.

This module opens a pool of connections to the media DC and keeps them warm, so
many chunks are in flight at once and the handshake is paid once per DC instead
of once per file. Throughput then scales with ``TURBO_STREAMS`` until the link
or Telegram's own per-connection limit is the bottleneck.

Both halves degrade safely: if anything goes wrong the caller falls back to
pyrogram's own implementation.
"""

import asyncio
import inspect
import logging
import math
import os

from pyrogram import raw
from pyrogram.errors import AuthBytesInvalid, FloodWait
from pyrogram.file_id import FileId, FileType
from pyrogram.session import Auth, Session

log = logging.getLogger(__name__)

# upload.GetFile: limit must divide 1 MB and be a multiple of 4096, and a part
# may not straddle a 1 MB boundary. 1 MB offsets satisfy all three.
DOWNLOAD_PART = 1024 * 1024

# upload.SaveBigFilePart: 524288 must be divisible by the part size.
UPLOAD_PART = 512 * 1024

# Telegram accepts at most 8000 parts for a big file.
MAX_PARTS = 8000

# Below this, one round trip is already enough and the pool is not worth it.
MIN_TURBO_SIZE = 5 * 1024 * 1024
BIG_FILE_THRESHOLD = 10 * 1024 * 1024

_pools = {}
_pool_lock = asyncio.Lock()

# Why turbo last declined, so a slow run can be explained instead of guessed at.
STATE = {'ready': False, 'streams': 0, 'dc': None, 'last_error': None, 'fallbacks': 0}


def note_failure(reason):
    STATE['last_error'] = reason
    STATE['fallbacks'] += 1
    log.warning(f'turbo fell back to pyrogram -> {reason}')


def status_line():
    if STATE['ready']:
        line = f"✅ {STATE['streams']} streams on DC {STATE['dc']}"
    else:
        line = '❌ not active (using plain pyrogram)'
    if STATE['last_error']:
        line += f"\nLast fallback ({STATE['fallbacks']}x): `{STATE['last_error'][:160]}`"
    return line


def _pwrite(fd, data, offset):
    if hasattr(os, 'pwrite'):
        os.pwrite(fd, data, offset)
    else:                       # Windows
        os.lseek(fd, offset, os.SEEK_SET)
        os.write(fd, data)


def _pread(fd, length, offset):
    if hasattr(os, 'pread'):
        return os.pread(fd, length, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, length)


async def _report(progress, args, current, total):
    if not progress:
        return
    try:
        result = progress(current, total, *args)
        if inspect.isawaitable(result):
            await result
    except Exception:
        pass


class Pool:
    """A set of warm media connections to one DC, shared by every transfer."""

    def __init__(self, client, dc_id, size):
        self.client = client
        self.dc_id = dc_id
        self.size = size
        self.sessions = []
        self.lock = asyncio.Lock()

    async def build(self):
        async with self.lock:
            if self.sessions:
                return self.sessions

            home = await self.client.storage.dc_id()
            test_mode = await self.client.storage.test_mode()

            if self.dc_id == home:
                auth_key = await self.client.storage.auth_key()
            else:
                auth_key = await Auth(self.client, self.dc_id, test_mode).create()

            sessions = []
            for index in range(self.size):
                try:
                    session = Session(self.client, self.dc_id, auth_key, test_mode, is_media=True)
                    await session.start()

                    # The whole pool shares one auth key, so the key only has to
                    # be authorised on the foreign DC once - but it must succeed,
                    # or none of the others can read anything.
                    if self.dc_id != home and index == 0:
                        await self._authorise(session)

                    sessions.append(session)
                except Exception as e:
                    if index == 0:
                        for opened in sessions:
                            try:
                                await opened.stop()
                            except Exception:
                                pass
                        raise
                    # A few connections short is still much faster than one.
                    log.warning(f'turbo: only {len(sessions)}/{self.size} streams on '
                                f'DC {self.dc_id} ({type(e).__name__}: {e})')
                    break

            self.sessions = sessions
            STATE.update({'ready': True, 'streams': len(sessions), 'dc': self.dc_id})
            log.info(f'turbo pool ready: {len(sessions)} streams on DC {self.dc_id}')
            return sessions

    async def _authorise(self, session):
        for _ in range(3):
            exported = await self.client.invoke(
                raw.functions.auth.ExportAuthorization(dc_id=self.dc_id)
            )
            try:
                await session.invoke(raw.functions.auth.ImportAuthorization(
                    id=exported.id, bytes=exported.bytes
                ))
                return
            except AuthBytesInvalid:
                continue
        raise AuthBytesInvalid

    async def close(self):
        async with self.lock:
            for session in self.sessions:
                try:
                    await session.stop()
                except Exception:
                    pass
            self.sessions = []


async def get_pool(client, dc_id, size):
    key = (id(client), dc_id)
    async with _pool_lock:
        pool = _pools.get(key)
        if pool is None or pool.size != size:
            if pool is not None:
                await pool.close()
            pool = Pool(client, dc_id, size)
            _pools[key] = pool
    return await pool.build()


async def close_pools():
    for pool in list(_pools.values()):
        await pool.close()
    _pools.clear()


def get_media(message):
    for attr in ('video', 'document', 'audio', 'photo', 'voice', 'video_note', 'animation', 'sticker'):
        media = getattr(message, attr, None)
        if media is not None:
            return media
    return None


def _location(file_id: FileId):
    if file_id.file_type == FileType.PHOTO:
        return raw.types.InputPhotoFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )
    return raw.types.InputDocumentFileLocation(
        id=file_id.media_id,
        access_hash=file_id.access_hash,
        file_reference=file_id.file_reference,
        thumb_size=file_id.thumbnail_size,
    )


async def turbo_download(client, message, dest, streams, progress=None, progress_args=()):
    """Download a message's media with ``streams`` connections at once.

    Returns the path, or None when the caller should use pyrogram's downloader.
    """
    media = get_media(message)
    file_size = getattr(media, 'file_size', 0) or 0
    if not media or file_size < MIN_TURBO_SIZE:
        return None

    # Everything below has to stay inside a guard. Returning None means "use
    # pyrogram instead"; anything that escapes this function instead becomes a
    # failed post, and the media silently never arrives.
    try:
        file_id = FileId.decode(media.file_id)
        location = _location(file_id)
        sessions = await get_pool(client, file_id.dc_id, streams)
    except Exception as e:
        note_failure(f'pool/setup: {type(e).__name__}: {e}')
        return None

    if not sessions:
        # With no connections the loop below would write nothing and still
        # report success, handing back a file of the right size full of zeros.
        note_failure('pool/setup: no usable connections')
        return None

    total_parts = math.ceil(file_size / DOWNLOAD_PART)
    parts = iter(range(total_parts))
    moved = 0
    failure = None

    os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
    fd = os.open(dest, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
    try:
        os.ftruncate(fd, file_size)

        async def worker(session):
            nonlocal moved, failure
            while failure is None:
                # asyncio is single threaded and there is no await in between,
                # so handing out part numbers this way is race free.
                index = next(parts, None)
                if index is None:
                    return

                offset = index * DOWNLOAD_PART
                for attempt in range(5):
                    try:
                        result = await session.invoke(
                            raw.functions.upload.GetFile(
                                location=location, offset=offset, limit=DOWNLOAD_PART
                            ),
                            sleep_threshold=30,
                        )
                        break
                    except FloodWait as e:
                        await asyncio.sleep(e.value + 1)
                    except Exception as e:
                        if attempt == 4:
                            failure = e
                            return
                        await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    failure = RuntimeError('chunk retries exhausted')
                    return

                if not isinstance(result, raw.types.upload.File):
                    failure = RuntimeError('CDN redirect not supported')
                    return

                chunk = result.bytes
                if not chunk:
                    continue

                _pwrite(fd, chunk, offset)
                moved += len(chunk)
                await _report(progress, progress_args, min(moved, file_size), file_size)

        await asyncio.gather(*(worker(s) for s in sessions[:streams]))

        if failure is not None:
            raise failure
        if moved < file_size:
            raise RuntimeError(f'incomplete: got {moved} of {file_size} bytes')
    except Exception as e:
        os.close(fd)
        try:
            os.remove(dest)
        except OSError:
            pass
        note_failure(f'download: {type(e).__name__}: {e}')
        return None
    else:
        os.close(fd)

    await _report(progress, progress_args, file_size, file_size)
    return dest


async def turbo_upload(client, path, streams, progress=None, progress_args=()):
    """Upload a file with ``streams`` connections at once.

    Returns an InputFileBig ready for send_*, or None to fall back.
    """
    file_size = os.path.getsize(path)
    if file_size <= BIG_FILE_THRESHOLD:
        # Small files need an md5 checksum, which is inherently sequential.
        return None

    total_parts = math.ceil(file_size / UPLOAD_PART)
    if total_parts > MAX_PARTS:
        return None

    try:
        home = await client.storage.dc_id()
        sessions = await get_pool(client, home, streams)
    except Exception as e:
        note_failure(f'upload pool: {type(e).__name__}: {e}')
        return None

    if not sessions:
        note_failure('upload pool: no usable connections')
        return None

    file_id = client.rnd_id()
    parts = iter(range(total_parts))
    moved = 0
    failure = None

    fd = os.open(path, os.O_RDONLY)
    try:
        async def worker(session):
            nonlocal moved, failure
            while failure is None:
                index = next(parts, None)
                if index is None:
                    return

                chunk = _pread(fd, UPLOAD_PART, index * UPLOAD_PART)
                if not chunk:
                    continue

                for attempt in range(5):
                    try:
                        await session.invoke(
                            raw.functions.upload.SaveBigFilePart(
                                file_id=file_id,
                                file_part=index,
                                file_total_parts=total_parts,
                                bytes=chunk,
                            ),
                            sleep_threshold=30,
                        )
                        break
                    except FloodWait as e:
                        await asyncio.sleep(e.value + 1)
                    except Exception as e:
                        if attempt == 4:
                            failure = e
                            return
                        await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    failure = RuntimeError('part retries exhausted')
                    return

                moved += len(chunk)
                await _report(progress, progress_args, min(moved, file_size), file_size)

        await asyncio.gather(*(worker(s) for s in sessions[:streams]))

        if failure is not None:
            raise failure
        if moved < file_size:
            raise RuntimeError(f'incomplete: sent {moved} of {file_size} bytes')
    except Exception as e:
        note_failure(f'upload: {type(e).__name__}: {e}')
        return None
    finally:
        os.close(fd)

    await _report(progress, progress_args, file_size, file_size)
    return raw.types.InputFileBig(
        id=file_id,
        parts=total_parts,
        name=os.path.basename(path),
    )


def install(client, streams):
    """Route every send_* upload through the parallel uploader.

    send_video and friends all funnel into Client.save_file, so replacing that
    one method speeds up every upload without touching a single call site.
    """
    if getattr(client, '_turbo_installed', False):
        return
    original = client.save_file

    async def save_file(path, file_id=None, file_part=0, progress=None, progress_args=()):
        # Already uploaded while it was being downloaded - nothing left to send.
        if isinstance(path, str) and file_id is None and file_part == 0:
            done = take_upload(path)
            if done is not None:
                await _report(progress, progress_args, 1, 1)
                return done

        # Resumed or partial uploads keep pyrogram's own bookkeeping.
        if isinstance(path, str) and file_id is None and file_part == 0:
            try:
                result = await turbo_upload(client, path, streams, progress, progress_args)
                if result is not None:
                    return result
            except Exception as e:
                note_failure(f'save_file: {type(e).__name__}: {e}')
        return await original(
            path, file_id=file_id, file_part=file_part,
            progress=progress, progress_args=progress_args,
        )

    client.save_file = save_file
    client._turbo_installed = True


async def warmup(client, streams):
    """Build the home-DC pool at startup.

    Without this a broken turbo is invisible: it just declines quietly on every
    transfer and everything runs at plain pyrogram speed. Doing it once up front
    puts the answer in the first few lines of the log.
    """
    try:
        dc_id = await client.storage.dc_id()
        sessions = await get_pool(client, dc_id, streams)
        print(f'Turbo ready: {len(sessions)} streams on DC {dc_id} 🚀')
        return True
    except Exception as e:
        STATE['ready'] = False
        STATE['last_error'] = f'warmup: {type(e).__name__}: {e}'
        print(f'Turbo UNAVAILABLE ({type(e).__name__}: {e}) - '
              'transfers will fall back to plain pyrogram')
        return False


# ── pipelined transfer ──────────────────────────────────────────────────────────
#
# Downloading a file completely and only then uploading it means the wall clock
# is download + upload. Telegram throttles per account, so more streams stop
# helping once that ceiling is hit - but the two directions are throttled
# separately, so running them at the same time roughly halves the total.
#
# Each 1 MB download part is exactly two 512 KB upload parts, and
# SaveBigFilePart accepts parts in any order, so a part can go out the moment it
# has arrived. Chunks are handed over in memory; the bounded queue means a slow
# upload simply backpressures the download instead of piling up.

_uploaded = {}


def register_upload(path, input_file):
    _uploaded[os.path.abspath(path)] = input_file


def take_upload(path):
    return _uploaded.pop(os.path.abspath(path), None)


def rekey_upload(old_path, new_path):
    handle = _uploaded.pop(os.path.abspath(old_path), None)
    if handle is not None:
        handle.name = os.path.basename(new_path)
        _uploaded[os.path.abspath(new_path)] = handle


async def pipe_transfer(src, dst, message, dest, streams, on_down=None, on_up=None):
    """Download and upload a file at the same time.

    Returns the path on success, having already uploaded it, or None to let the
    caller do things the ordinary way.
    """
    media = get_media(message)
    file_size = getattr(media, 'file_size', 0) or 0
    if not media or file_size <= BIG_FILE_THRESHOLD:
        return None
    if file_size > 2000 * 1024 * 1024:
        return None                      # the >2GB path uploads via the userbot

    up_total = math.ceil(file_size / UPLOAD_PART)
    if up_total > MAX_PARTS:
        return None

    try:
        file_id = FileId.decode(media.file_id)
        location = _location(file_id)
        down_sessions = await get_pool(src, file_id.dc_id, streams)
        up_sessions = await get_pool(dst, await dst.storage.dc_id(), streams)
    except Exception as e:
        note_failure(f'pipe setup: {type(e).__name__}: {e}')
        return None

    if not down_sessions or not up_sessions:
        note_failure('pipe setup: no usable connections')
        return None

    down_total = math.ceil(file_size / DOWNLOAD_PART)
    parts = iter(range(down_total))
    upload_id = dst.rnd_id()
    queue = asyncio.Queue(maxsize=max(2, streams * 2))
    downloaded = uploaded = 0
    failure = None

    os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
    fd = os.open(dest, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)

    async def down_worker(session):
        nonlocal downloaded, failure
        while failure is None:
            index = next(parts, None)
            if index is None:
                return
            offset = index * DOWNLOAD_PART
            for attempt in range(5):
                try:
                    result = await session.invoke(raw.functions.upload.GetFile(
                        location=location, offset=offset, limit=DOWNLOAD_PART,
                    ), sleep_threshold=30)
                    break
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                except Exception as e:
                    if attempt == 4:
                        failure = e
                        return
                    await asyncio.sleep(0.5 * (attempt + 1))
            else:
                failure = RuntimeError('chunk retries exhausted')
                return

            if not isinstance(result, raw.types.upload.File):
                failure = RuntimeError('CDN redirect not supported')
                return

            chunk = result.bytes
            if not chunk:
                continue

            _pwrite(fd, chunk, offset)
            downloaded += len(chunk)
            await _report(on_down, (), min(downloaded, file_size), file_size)

            for step in range(0, len(chunk), UPLOAD_PART):
                await queue.put((index * 2 + step // UPLOAD_PART,
                                 chunk[step:step + UPLOAD_PART]))

    async def up_worker(session):
        nonlocal uploaded, failure
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                if failure is not None:
                    continue
                part_index, data = item
                for attempt in range(5):
                    try:
                        await session.invoke(raw.functions.upload.SaveBigFilePart(
                            file_id=upload_id, file_part=part_index,
                            file_total_parts=up_total, bytes=data,
                        ), sleep_threshold=30)
                        break
                    except FloodWait as e:
                        await asyncio.sleep(e.value + 1)
                    except Exception as e:
                        if attempt == 4:
                            failure = e
                            return
                        await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    failure = RuntimeError('part retries exhausted')
                    return
                uploaded += len(data)
                await _report(on_up, (), min(uploaded, file_size), file_size)
            finally:
                queue.task_done()

    try:
        uploaders = [asyncio.create_task(up_worker(s)) for s in up_sessions[:streams]]
        await asyncio.gather(*(down_worker(s) for s in down_sessions[:streams]))
        for _ in uploaders:
            await queue.put(None)
        await asyncio.gather(*uploaders)

        if failure is not None:
            raise failure
        if downloaded < file_size or uploaded < file_size:
            raise RuntimeError(
                f'incomplete: down {downloaded}, up {uploaded}, need {file_size}')
    except Exception as e:
        for task in uploaders:
            task.cancel()
        os.close(fd)
        try:
            os.remove(dest)
        except OSError:
            pass
        note_failure(f'pipe: {type(e).__name__}: {e}')
        return None
    else:
        os.close(fd)

    register_upload(dest, raw.types.InputFileBig(
        id=upload_id, parts=up_total, name=os.path.basename(dest)))
    return dest
