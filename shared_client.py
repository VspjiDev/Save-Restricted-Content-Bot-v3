# Copyright (c) 2025 Vsp Official
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

import asyncio
import sys

from pyrogram import Client

from config import API_ID, API_HASH, BOT_TOKEN, STRING, TURBO_DISABLED, TURBO_STREAMS, WORKERS
from utils import turbo


def bind_loop(client):
    """Point a client at the loop that is actually running.

    Client.__init__ and Dispatcher.__init__ both call asyncio.get_event_loop()
    and keep the result. These clients are built at import time, when no loop is
    running, so they latch onto a throwaway loop that never runs - and then
    add_handler schedules its work there and it never happens, leaving the bot
    connected but deaf to every message. Rebinding once, from inside the real
    loop, is what keeps the handlers alive.
    """
    if client is None:
        return client
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return client          # not in a loop yet; start_client() will do it
    client.loop = loop
    dispatcher = getattr(client, 'dispatcher', None)
    if dispatcher is not None:
        dispatcher.loop = loop
    return client


def build_client(name, **kwargs):
    kwargs.setdefault('api_id', API_ID)
    kwargs.setdefault('api_hash', API_HASH)
    kwargs.setdefault('sleep_threshold', 60)   # sit out short FloodWaits
    # Pyrogram's own knob only caps how many files move at once, not how fast a
    # single one moves. utils/turbo.py is what makes an individual transfer fast.
    kwargs.setdefault('max_concurrent_transmissions', max(1, WORKERS))
    try:
        client = Client(name, **kwargs)
    except TypeError:
        kwargs.pop('max_concurrent_transmissions', None)
        client = Client(name, **kwargs)
    return bind_loop(client)


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
    # Must happen before start(), so the dispatcher runs on the live loop.
    bind_loop(app)
    bind_loop(userbot)

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
        # Prove the pool works now rather than discovering it file by file.
        await turbo.warmup(app, TURBO_STREAMS)

    return app, userbot
