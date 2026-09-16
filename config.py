# Copyright (c) 2025 devgagan : https://github.com/devgaganin.
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

import os
from dotenv import load_dotenv
load_dotenv()

# ════════════════════════════════════════════════════════════════════════════════
# ░ CONFIGURATION SETTINGS  --  restricted content forwarder
# ════════════════════════════════════════════════════════════════════════════════

# ─── BOT / DATABASE CONFIG ──────────────────────────────────────────────────────
API_ID       = int(os.getenv("API_ID", "20821267"))
API_HASH     = os.getenv("API_HASH", "8723cdf433be176300044547ee6bab7a")
BOT_TOKEN    = os.getenv("BOT_TOKEN", "8234233632:AAEVB1VaK5oT8gk_raQaew0vyFh176ULNtI")
MONGO_DB     = os.getenv("MONGO_DB", "mongodb+srv://Black_Horse:BlackHorse@blackhorse.ji3jgx3.mongodb.net/BlackHorses?retryWrites=true&w=majority")
DB_NAME      = os.getenv("DB_NAME", "telegram_downloader")

# ─── OWNER / CONTROL SETTINGS ───────────────────────────────────────────────────
OWNER_ID     = list(map(int, os.getenv("OWNER_ID", "7894095051").split()))  # space-separated list
STRING       = os.getenv("STRING", None)  # optional session string
LOG_GROUP    = int(os.getenv("LOG_GROUP", "-1003132690051"))
FORCE_SUB    = int(os.getenv("FORCE_SUB", "0"))  # 0 = disabled

# ─── SECURITY KEYS ──────────────────────────────────────────────────────────────
MASTER_KEY   = os.getenv("MASTER_KEY", "gK8HzLfT9QpViJcYeB5wRa3DmN7P2xUq")  # session encryption
IV_KEY       = os.getenv("IV_KEY", "s7Yx5CpVmE3F")  # decryption key

# ─── UI / LINKS ─────────────────────────────────────────────────────────────────
JOIN_LINK     = os.getenv("JOIN_LINK", "https://t.me/team_spy_pro")

# ════════════════════════════════════════════════════════════════════════════════
# ░ SPEED / PERFORMANCE KNOBS  🚀
# ════════════════════════════════════════════════════════════════════════════════

# How many files are downloaded in parallel inside one batch. Uploads always stay
# in order, only the downloads run ahead of the uploader.
WORKERS = int(os.getenv("WORKERS", "4"))

# Parallel chunk streams pyrogram opens per file transfer. This is the single
# biggest download/upload speed lever. 8-16 is a good range on a decent VPS.
MAX_TRANSMISSIONS = int(os.getenv("MAX_TRANSMISSIONS", "8"))

# Cooldown between two messages of a batch, in seconds. 0 is fastest; raise it
# only if Telegram starts throwing FloodWait at you.
BATCH_DELAY = float(os.getenv("BATCH_DELAY", "0"))

# How often the live progress message is edited (seconds). Every edit costs an
# API round trip, so a lower value looks nicer but transfers slightly slower.
PROGRESS_INTERVAL = float(os.getenv("PROGRESS_INTERVAL", "6"))

# Max messages allowed in a single /batch run.
BATCH_LIMIT = int(os.getenv("BATCH_LIMIT", "5000"))

# Where temporary media is written.
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")

# Port for the tiny keep-alive HTTP server (Koyeb / Render / Heroku web dynos).
PORT = int(os.getenv("PORT", "8080"))

