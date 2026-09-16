# Copyright (c) 2025 Vsp Official
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

import os
import sys

from dotenv import load_dotenv

load_dotenv()

# ════════════════════════════════════════════════════════════════════════════════
# ░ CONFIGURATION SETTINGS  --  restricted content forwarder
# ════════════════════════════════════════════════════════════════════════════════
#
# Every credential comes from the environment. Nothing secret is hardcoded here:
# this file is committed, so anything written into it is public.
# Put your values in a .env file (it is gitignored) or in your host's env vars.


_MISSING = []
_BAD = []


def _need(name, hint=''):
    value = os.getenv(name, '').strip()
    if not value:
        _MISSING.append(f'  {name:<12} {hint}')
    return value


def _int(name, default=0):
    """Read an optional integer setting without crash-looping on a typo."""
    raw = (os.getenv(name) or '').strip().replace(' ', '')
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _BAD.append(f'  {name:<12} must be a whole number, got {raw!r}')
        return default


def _ids(name, raw):
    """Accept '123', '123 456' and '123,456' alike - people paste all three."""
    parts = [p for p in raw.replace(',', ' ').split() if p]
    try:
        return [int(p) for p in parts]
    except ValueError:
        _BAD.append(f'  {name:<12} must be numeric user id(s), got {raw!r}')
        return []

# ─── BOT / DATABASE CONFIG ──────────────────────────────────────────────────────
_API_ID      = _need('API_ID', 'from https://my.telegram.org')
API_HASH     = _need('API_HASH', 'from https://my.telegram.org')
BOT_TOKEN    = _need('BOT_TOKEN', 'from @BotFather')
MONGO_DB     = _need('MONGO_DB', 'MongoDB connection URL')
DB_NAME      = os.getenv('DB_NAME', 'telegram_downloader')

# ─── OWNER / CONTROL SETTINGS ───────────────────────────────────────────────────
_OWNER_ID    = _need('OWNER_ID', 'your Telegram user id (space separated for several)')
STRING       = (os.getenv('STRING') or '').strip() or None  # premium session, >2GB uploads
LOG_GROUP    = _int('LOG_GROUP')                    # staging chat for >2GB uploads
FORCE_SUB    = _int('FORCE_SUB')                    # 0 = disabled

# ─── SECURITY KEYS ──────────────────────────────────────────────────────────────
# Used to encrypt the session strings stored in MongoDB. Pick your own random
# values and keep them stable: changing them makes every stored login unreadable,
# so users would have to /login again.
MASTER_KEY   = _need('MASTER_KEY', '32 random characters, session encryption')
IV_KEY       = _need('IV_KEY', '12 random characters, session encryption')

API_ID   = _int('API_ID')
OWNER_ID = _ids('OWNER_ID', _OWNER_ID)

if _MISSING or _BAD:
    report = ['Cannot start — check your config.\n']
    if _MISSING:
        report.append('Missing required settings:\n' + '\n'.join(_MISSING) + '\n')
    if _BAD:
        report.append('Invalid settings:\n' + '\n'.join(_BAD) + '\n')
    report.append(
        'On Heroku set these under Settings -> Config Vars, then restart the dyno.\n'
        'Running locally? Copy .env.example to .env and fill it in.\n'
    )
    sys.exit('\n'.join(report))

# ─── BRANDING ───────────────────────────────────────────────────────────────────
BRAND     = os.getenv('BRAND', 'Vsp Official')
JOIN_LINK = os.getenv('JOIN_LINK', '')   # optional updates channel shown on /start

# ════════════════════════════════════════════════════════════════════════════════
# ░ SPEED / PERFORMANCE KNOBS  🚀
# ════════════════════════════════════════════════════════════════════════════════

# How many files are downloaded in parallel inside one batch. Uploads always stay
# in order, only the downloads run ahead of the uploader.
WORKERS = int(os.getenv('WORKERS', '4'))

# Connections opened per file by the turbo engine (utils/turbo.py). Pyrogram on
# its own moves one chunk at a time over one connection, which caps a transfer at
# about one 1 MB round trip; this is what actually multiplies throughput.
# Roughly linear: at a 200 ms round trip, 1 stream gives ~5 MB/s and 16 gives
# ~75 MB/s. Drop it if Telegram starts answering with FloodWait.
TURBO_STREAMS = int(os.getenv('TURBO_STREAMS', '16'))

# Set to 1 to disable the turbo engine and use plain pyrogram transfers.
TURBO_DISABLED = os.getenv('TURBO_DISABLED', '0') == '1'

# Cooldown between two messages of a batch, in seconds. 0 is fastest; raise it
# only if Telegram starts throwing FloodWait at you.
BATCH_DELAY = float(os.getenv('BATCH_DELAY', '0'))

# How often the live progress message is edited (seconds). Every edit costs an
# API round trip, so a lower value looks nicer but transfers slightly slower.
PROGRESS_INTERVAL = float(os.getenv('PROGRESS_INTERVAL', '6'))

# Max messages allowed in a single /batch run.
BATCH_LIMIT = int(os.getenv('BATCH_LIMIT', '10000'))

# Where temporary media is written.
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', 'downloads')

# Port for the tiny keep-alive HTTP server (Koyeb / Render / Heroku web dynos).
PORT = int(os.getenv('PORT', '8080'))
