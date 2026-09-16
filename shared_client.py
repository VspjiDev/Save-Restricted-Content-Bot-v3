# Copyright (c) 2025 Vsp Official
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

import sys

from pyrogram import Client

from config import API_ID, API_HASH, BOT_TOKEN, STRING, TURBO_DISABLED, TURBO_STREAMS, WORKERS
from utils import turbo


def build_client(name, **kwargs):
    kwargs.setdefault('api_id', API_ID)
    kwargs.setdefault('api_hash', API_HASH)
    kwargs.setdefault('sleep_threshold', 60)   # sit out short FloodWaits
    # Pyrogram's own knob only caps how many files move at once, not how fast a
    # single one moves. utils/turbo.py is what makes an individual transfer fast.
    kwargs.setdefault('max_concurrent_transmissions', max(1, WORKERS))
    try:
        return Client(name, **kwargs)
    except TypeError:
        kwargs.pop('max_concurrent_transmissions', None)
        return Client(name, **kwargs)


def turbocharge(client):
    """Route this client's uploads through the parallel engine."""
    if client and not TURBO_DISABLED:
        turbo.install(client, TURBO_STREAMS)
    return client


# the one bot everything runs through - it receives commands and uploads files
app = build_client('pyrogrambot', bot_token=BOT_TOKEN, workers=16)

# optional premium userbot, only needed to push files above 2 GB
userbot = build_client('4gbbot', session_string=STRING, no_updates=True) if STRING else None


async def start_client():
    await app.start()
    turbocharge(app)
    print('Bot started...')

    if userbot:
        try:
            await userbot.start()
            turbocharge(userbot)
            print('Userbot started (uploads above 2GB enabled)...')
        except Exception as e:
            print(f'Invalid or expired STRING session, 4GB uploads disabled: {e}')
            sys.exit(1)

    if not TURBO_DISABLED:
        print(f'Turbo transfers enabled: {TURBO_STREAMS} streams per file 🚀')

    return app, userbot
