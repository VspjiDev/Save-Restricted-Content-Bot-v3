# Save Restricted Content Bot v3 🚀

A focused Telegram bot that does exactly one thing: **save and forward posts from
restricted private and public channels**, as fast as Telegram allows.

Everything that was not about forwarding — the YouTube/Instagram downloader,
Telegram Stars payments, premium plans, referral/plan/terms pages and the second
Telethon client — has been removed.

---

## What it does

| Command | What it does |
|---|---|
| `/start` | Intro and quick setup |
| `/single` | Extract one post from a link |
| `/batch` | Bulk extract starting at a link |
| `/stop` | Cancel the running batch |
| `/setbot <token>` | Add your own bot — it does all the uploading |
| `/rembot` | Remove your upload bot |
| `/login` | Log in so private channels can be read |
| `/logout` | Remove your session |
| `/settings` | Target chat, caption, rename tag, word rules, thumbnail |
| `/status` | Your login / bot / target status |
| `/set` | (owner) push the command list to BotFather |

Supported links:

```
public   https://t.me/channel/123
private  https://t.me/c/1234567890/123
topic    https://t.me/c/1234567890/12/123
bot chat https://t.me/b/botname/123
```

---

## Why it is fast 🚀

| | Before | Now |
|---|---|---|
| Pause between posts | `sleep(10)` — hardcoded | `BATCH_DELAY`, default **0** |
| Downloads | one at a time | **`WORKERS` in parallel**, uploads stay in order |
| Chunks per transfer | 1 stream | **`MAX_TRANSMISSIONS`** parallel streams (default 8) |
| Fetching 100 posts | 100 API calls | **1 bulk call** |
| Peer resolution | up to 200 dialogs walked *per post* | cached, refreshed at most every 10 min |
| Progress updates | an edit per file, awaited inside the transfer | one throttled status message, never blocks a transfer |
| Settings lookups | 4+ Mongo reads per post | 1 cached read per run |
| Video metadata | OpenCV decoded the file | `ffprobe` reads the header only |
| Unprotected posts | downloaded and re-uploaded | **copied server side — zero bytes transferred** |
| Event loop | asyncio default | `uvloop` when available |

On a 20-post batch where each post takes 1s to download and 0.5s to upload, the
old serial flow needed ~233s; the new pipeline finishes the same work in ~11s
because uploads and downloads overlap and the 10s pause is gone.

The pipeline downloads ahead while uploading strictly in order, so **posts arrive
in the same order they appear in the source channel**.

---

## Deploy

```bash
git clone <this repo> && cd Save-Restricted-Content-Bot-v3
pip install -r requirements.txt
# set your env vars (see below), then
python3 main.py
```

Docker:

```bash
docker build -t srcb . && docker run --env-file .env srcb
```

`ffmpeg` is required for video thumbnails and duration — the Dockerfile installs
it. Without it the bot still works, just without generated thumbnails.

### Environment variables

**Required**

| Var | Meaning |
|---|---|
| `API_ID`, `API_HASH` | from https://my.telegram.org |
| `BOT_TOKEN` | from @BotFather |
| `MONGO_DB` | MongoDB connection URL |
| `OWNER_ID` | your user id (space separated for several) |
| `MASTER_KEY`, `IV_KEY` | session encryption keys — **set your own** |

**Optional**

| Var | Default | Meaning |
|---|---|---|
| `STRING` | – | Premium account session string, needed only for uploads above 2GB |
| `LOG_GROUP` | – | Channel used to stage those >2GB files; the bot must be admin |
| `FORCE_SUB` | `0` | Channel users must join, `0` disables it |
| `WORKERS` | `4` | Files downloaded in parallel |
| `MAX_TRANSMISSIONS` | `8` | Parallel chunk streams per file — the biggest speed lever |
| `BATCH_DELAY` | `0` | Seconds between posts; raise only if you hit FloodWait |
| `PROGRESS_INTERVAL` | `6` | Seconds between progress edits |
| `BATCH_LIMIT` | `5000` | Max posts per `/batch` |
| `DOWNLOAD_DIR` | `downloads` | Temp media directory |
| `PORT` | `8080` | Keep-alive HTTP port |

### Tuning

* Fast VPS, plenty of RAM → `WORKERS=6`, `MAX_TRANSMISSIONS=16`
* Small box or slow disk → `WORKERS=2`, `MAX_TRANSMISSIONS=4`
* Getting FloodWait → `BATCH_DELAY=2`, then raise it further if needed

Each parallel worker holds one file on disk, so peak temp usage is roughly
`WORKERS × largest file size`.

---

## Security note

`config.py` used to ship working `API_ID` / `API_HASH` / `BOT_TOKEN` / `MONGO_DB`
/ `MASTER_KEY` values as fallback defaults. They are gone — every credential now
comes from the environment and the bot refuses to start with a clear message if
one is missing.

**Those old values are still in the git history, so rotate them:** a new bot
token from @BotFather, new MongoDB credentials, and fresh `MASTER_KEY` /
`IV_KEY`. Note that changing the key pair makes stored logins unreadable, so
users will have to `/login` again.

Copy `.env.example` to `.env` and fill it in.

---

Licensed under the GNU General Public License v3.0 — see `LICENSE`.
