# Copyright (c) 2025 Vsp Official
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

"""Only one instance of the bot may handle updates.

Scaling the dyno count instead of setting the WORKERS config var starts several
copies of the bot on the same token. They all receive the same updates, so every
command is answered once per copy, and they all hammer the API at once until
Telegram answers with FloodWait. Nothing in the bot notices; it just looks
broken and costs a multiple of the bill.

A lock in MongoDB decides which copy is live. The rest keep their health
endpoint up so the platform leaves them alone, say clearly in the log what is
going on, and wait - if the live one dies, whichever is waiting takes over
within a lease.
"""

import asyncio
import os
import socket
import uuid
from datetime import datetime, timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from utils.func import db

LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 30

locks = db['instance_lock']
IDENTITY = f"{os.getenv('DYNO') or socket.gethostname()}:{uuid.uuid4().hex[:8]}"


async def _claim():
    """Take the lock, or renew it if we already hold it. False if someone else has it."""
    now = datetime.utcnow()
    try:
        await locks.find_one_and_update(
            {
                '_id': 'bot',
                '$or': [
                    {'expires_at': {'$lt': now}},   # the holder went away
                    {'owner': IDENTITY},            # still ours, just renewing
                ],
            },
            {'$set': {'owner': IDENTITY, 'expires_at': now + timedelta(seconds=LEASE_SECONDS)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return True
    except DuplicateKeyError:
        # The document exists and the filter did not match it, so the lease is
        # held by a live instance that is not us.
        return False
    except PyMongoError as e:
        # Never let a database hiccup stop the only running copy.
        print(f'Instance lock unavailable ({e}); continuing without it.')
        return True


async def current_holder():
    try:
        doc = await locks.find_one({'_id': 'bot'})
        return doc.get('owner') if doc else None
    except PyMongoError:
        return None


async def acquire(poll_seconds=20):
    """Block until this instance owns the lock."""
    announced = False
    while True:
        if await _claim():
            if announced:
                print(f'Taking over as the live instance ({IDENTITY}).')
            return True

        if not announced:
            announced = True
            holder = await current_holder()
            print(
                '=' * 68 + '\n'
                f'ANOTHER INSTANCE IS ALREADY RUNNING ({holder}).\n'
                'This copy will stay idle so commands are not answered twice and\n'
                'Telegram does not throttle the token.\n\n'
                'You have scaled the dyno count above 1. To run more transfers in\n'
                'parallel set the WORKERS config var instead, and scale dynos\n'
                'back to 1 under Resources.\n'
                + '=' * 68
            )
        await asyncio.sleep(poll_seconds)


async def heartbeat():
    """Keep renewing the lease for as long as this instance is alive."""
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        if not await _claim():
            print('Lost the instance lock - another copy has taken over. Standing down.')
            return


async def release():
    try:
        await locks.delete_one({'_id': 'bot', 'owner': IDENTITY})
    except PyMongoError:
        pass
