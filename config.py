# Copyright (c) 2025 devgagan : https://github.com/devgaganin.
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


def _need(name, hint=''):
    value = os.getenv(name, '').strip()
    if not value:
        _MISSING.append(f'  {name:<12} {hint}')
    return value


_MISSING = []

# ─── BOT / DATABASE CONFIG ──────────────────────────────────────────────────────
_API_ID      = _need('API_ID', 'from https://my.telegram.org')
API_HASH     = _need('API_HASH', 'from https://my.telegram.org')
BOT_TOKEN    = _need('BOT_TOKEN', 'from @BotFather')
MONGO_DB     = _need('MONGO_DB', 'MongoDB connection URL')
DB_NAME      = os.getenv('DB_NAME', 'telegram_downloader')

# ─── OWNER / CONTROL SETTINGS ───────────────────────────────────────────────────
_OWNER_ID    = _need('OWNER_ID', 'your Telegram user id (space separated for several)')
STRING       = os.getenv('STRING') or None          # premium session, only for >2GB uploads
LOG_GROUP    = int(os.getenv('LOG_GROUP') or 0)     # staging chat for >2GB uploads
FORCE_SUB    = int(os.getenv('FORCE_SUB') or 0)     # 0 = disabled

# ─── SECURITY KEYS ──────────────────────────────────────────────────────────────
# Used to encrypt the session strings stored in MongoDB. Pick your own random
# values and keep them stable: changing them makes every stored login unreadable,
# so users would have to /login again.
MASTER_KEY   = _need('MASTER_KEY', '32 random characters, session encryption')
IV_KEY       = _need('IV_KEY', '12 random characters, session encryption')

if _MISSING:
    sys.exit(
        'Missing required environment variables:\n\n'
        + '\n'.join(_MISSING)
        + '\n\nSet them in a .env file or your host config, then start again.\n'
    )

API_ID   = int(_API_ID)
OWNER_ID = list(map(int, _OWNER_ID.split()))

# ─── UI / LINKS ─────────────────────────────────────────────────────────────────
JOIN_LINK = os.getenv('JOIN_LINK', 'https://t.me/team_spy_pro')

# ════════════════════════════════════════════════════════════════════════════════
# ░ SPEED / PERFORMANCE KNOBS  🚀
# ════════════════════════════════════════════════════════════════════════════════

# How many files are downloaded in parallel inside one batch. Uploads always stay
# in order, only the downloads run ahead of the uploader.
WORKERS = int(os.getenv('WORKERS', '4'))

# Parallel chunk streams pyrogram opens per file transfer. This is the single
# biggest download/upload speed lever. 8-16 is a good range on a decent VPS.
MAX_TRANSMISSIONS = int(os.getenv('MAX_TRANSMISSIONS', '8'))

# Cooldown between two messages of a batch, in seconds. 0 is fastest; raise it
# only if Telegram starts throwing FloodWait at you.
BATCH_DELAY = float(os.getenv('BATCH_DELAY', '0'))

# How often the live progress message is edited (seconds). Every edit costs an
# API round trip, so a lower value looks nicer but transfers slightly slower.
PROGRESS_INTERVAL = float(os.getenv('PROGRESS_INTERVAL', '6'))

# Max messages allowed in a single /batch run.
BATCH_LIMIT = int(os.getenv('BATCH_LIMIT', '5000'))

# Where temporary media is written.
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', 'downloads')

# Port for the tiny keep-alive HTTP server (Koyeb / Render / Heroku web dynos).
PORT = int(os.getenv('PORT', '8080'))
