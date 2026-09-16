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
            try:
                for index in range(self.size):
                    session = Session(self.client, self.dc_id, auth_key, test_mode, is_media=True)
                    await session.start()

                    # The whole pool shares one auth key, so the key only has to
                    # be authorised on the foreign DC once.
                    if self.dc_id != home and index == 0:
                        await self._authorise(session)

                    sessions.append(session)
            except Exception:
                for session in sessions:
                    try:
                        await session.stop()
                    except Exception:
                        pass
                raise

            self.sessions = sessions
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

    try:
        file_id = FileId.decode(media.file_id)
    except Exception:
        return None

    location = _location(file_id)
    sessions = await get_pool(client, file_id.dc_id, streams)

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
    except Exception as e:
        os.close(fd)
        try:
            os.remove(dest)
        except OSError:
            pass
        log.warning(f'turbo download fell back to pyrogram: {e}')
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

    home = await client.storage.dc_id()
    sessions = await get_pool(client, home, streams)

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
    except Exception as e:
        log.warning(f'turbo upload fell back to pyrogram: {e}')
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
        # Resumed or partial uploads keep pyrogram's own bookkeeping.
        if isinstance(path, str) and file_id is None and file_part == 0:
            try:
                result = await turbo_upload(client, path, streams, progress, progress_args)
                if result is not None:
                    return result
            except Exception as e:
                log.warning(f'turbo upload error, using pyrogram: {e}')
        return await original(
            path, file_id=file_id, file_part=file_part,
            progress=progress, progress_args=progress_args,
        )

    client.save_file = save_file
    client._turbo_installed = True
