# Copyright (c) 2025 devgagan : https://github.com/devgaganin.
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

import sys

from pyrogram import Client

from config import API_ID, API_HASH, BOT_TOKEN, STRING, MAX_TRANSMISSIONS


def build_client(name, **kwargs):
    """Create a pyrogram client with the transfer tuning applied.

    ``max_concurrent_transmissions`` makes pyrogram open several chunk streams per
    file instead of one, which is where most of the download/upload speed comes
    from. It is guarded because older pyrogram builds do not accept the argument.
    """
    kwargs.setdefault('api_id', API_ID)
    kwargs.setdefault('api_hash', API_HASH)
    kwargs.setdefault('sleep_threshold', 60)      # auto-wait short FloodWaits
    kwargs.setdefault('max_concurrent_transmissions', max(1, MAX_TRANSMISSIONS))
    try:
        return Client(name, **kwargs)
    except TypeError:
        kwargs.pop('max_concurrent_transmissions', None)
        return Client(name, **kwargs)


# main bot - handles commands and uploads
app = build_client('pyrogrambot', bot_token=BOT_TOKEN, workers=16)

# optional premium userbot - used to push files bigger than 2 GB
userbot = build_client('4gbbot', session_string=STRING, no_updates=True) if STRING else None


async def start_client():
    await app.start()
    print('Pyro App Started...')

    if userbot:
        try:
            await userbot.start()
            print('Userbot started (4GB uploads enabled)...')
        except Exception as e:
            print(f'Invalid or expired STRING session, 4GB uploads disabled: {e}')
            sys.exit(1)

    return app, userbot
