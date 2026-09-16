# Vsp Official — Restricted Content Saver 🚀

[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/VspjiDev/Save-Restricted-Content-Bot-v3)

A Telegram bot that does exactly one thing: **save and forward posts from
restricted private and public channels**, as fast as the link allows.

Everything else — the YouTube/Instagram downloader, Telegram Stars payments,
premium plans, plan/terms pages, the per-user "add your own bot" flow and the
second Telethon client — has been removed. Uploads go out through this bot
itself; there is nothing extra to set up.

---

## Commands

| Command | What it does |
|---|---|
| `/start` | Intro and quick setup |
| `/single` | Extract one post from a link |
| `/batch` | Bulk extract starting at a link (up to 10,000 posts) |
| `/stop` | Cancel the running batch |
| `/login` | Log in — only needed for private channels |
| `/logout` | Remove your session |
| `/settings` | Target chat, caption, rename tag, word rules, thumbnail |
| `/status` | Your login, target chat and engine status |
| `/set` | (owner) push the command list to BotFather |

Supported links:

```
public   https://t.me/channel/123
private  https://t.me/c/1234567890/123
topic    https://t.me/c/1234567890/12/123
bot chat https://t.me/b/botname/123
```

---

## How a post is moved

1. **Public and not protected** → copied server side by file id. Nothing is
   downloaded or uploaded at all, so it is effectively instant.
2. **Everything else** (restricted public posts, private channels) → downloaded
   and re-uploaded through the turbo engine below.

---

## Why it is fast 🚀

**The turbo engine (`utils/turbo.py`)** is where the speed comes from.

Pyrogram moves **one 1 MB chunk at a time over a single connection**, and builds
a fresh session — auth handshake included — for *every file*. That caps a
transfer at roughly one chunk per round trip, which is why stock speeds sit in
the low single digits regardless of how fast your server is. (Pyrogram's own
`max_concurrent_transmissions` only limits how many *files* move at once; it
does nothing for a single file.)

Turbo keeps a warm pool of connections to the media DC and keeps many chunks in
flight, so throughput scales close to linearly with `TURBO_STREAMS`:

| Streams | Throughput at a 200 ms round trip |
|---:|---:|
| 1 (stock pyrogram) | ~5 MB/s |
| 4 | ~20 MB/s |
| 8 | ~40 MB/s |
| **16 (default)** | **~75 MB/s** |
| 24 | ~110 MB/s |

Measured against a simulated 200 ms link, so it shows how the engine scales, not
what your server will do — **real throughput is capped by your VPS bandwidth**.
60–70 MB/s needs a genuinely fast host (1 Gbps or better). On a small box you
will hit the box's limit long before the engine's.

Uploads get the same treatment: `Client.save_file` is replaced, so every
`send_video` / `send_document` call uses the parallel uploader without any
change at the call sites. If anything goes wrong, both halves fall back to
pyrogram's own implementation automatically.

**On top of that:**

| | Before | Now |
|---|---|---|
| Pause between posts | `sleep(10)` — hardcoded | `BATCH_DELAY`, default **0** |
| Parallel posts | one at a time | **`WORKERS` downloading**, uploads stay in order |
| Fetching 100 posts | 100 API calls | **1 bulk call** |
| Peer resolution | up to 200 dialogs walked *per post* | cached, refreshed at most every 10 min |
| Progress updates | an edit per file, awaited inside the transfer | one throttled status message, never blocks a transfer |
| Settings lookups | 4+ Mongo reads per post | 1 cached read per run |
| Video metadata | OpenCV decoded the file | `ffprobe` reads the header only |
| Event loop | asyncio default | `uvloop` when available |

Posts are downloaded ahead but uploaded strictly in order, so **they arrive in
the same order as the source channel**.

---

## Deploy

### Heroku (one click)

Click the button at the top. You will be asked for five things:

| Field | Where to get it |
|---|---|
| `API_ID`, `API_HASH` | https://my.telegram.org |
| `BOT_TOKEN` | @BotFather |
| `MONGO_DB` | a free cluster at https://cloud.mongodb.com |
| `OWNER_ID` | your numeric Telegram user id |

`MASTER_KEY` and `IV_KEY` are generated for you, everything else has a sensible
default. Deploy, and the bot starts on its own — the app runs as a `web` dyno
(the one the deploy button scales automatically) and serves a small status page
at your app URL, which doubles as a health check.

**Worth knowing before you deploy:**

* **Eco dynos sleep** after 30 minutes without a web request, and a sleeping bot
  answers nothing. Either use a Basic dyno (no sleeping) or point an uptime
  pinger at your app URL.
* **Heroku restarts every dyno about once a day.** A batch running at that
  moment stops where it is; just run it again.
* **The disk is small and temporary** (~1 GB, wiped on restart). `WORKERS`
  defaults to `2` on Heroku for that reason, since each worker holds one whole
  file while it uploads. Custom thumbnails are also lost on restart — logins are
  not, those live in MongoDB.
* **Bandwidth is the real speed limit.** The turbo engine will use whatever the
  dyno gives it, but a Heroku dyno is not a 1 Gbps box, so expect well under the
  numbers in the table above. A VPS gets closer to them.
* **Check `/status` if transfers feel slow.** It reports whether the turbo pool
  is actually up and, if it is not, the error that stopped it. The startup log
  says the same thing in its first few lines (`Turbo ready: N streams on DC X`).
* Changing `MASTER_KEY` or `IV_KEY` later makes every stored login unreadable,
  so users would have to `/login` again. Set them once and leave them alone.

### Local or VPS

```bash
cp .env.example .env     # fill it in
pip install -r requirements.txt
python3 main.py
```

### Docker

```bash
docker build -t vsp-saver . && docker run --env-file .env vsp-saver
```

`ffmpeg` is needed for video thumbnails and duration — the Dockerfile installs
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
| `TURBO_STREAMS` | `16` | Connections per file — the main speed lever |
| `TURBO_DISABLED` | `0` | Set to `1` to fall back to plain pyrogram transfers |
| `WORKERS` | `4` | Files downloaded in parallel |
| `BATCH_DELAY` | `0` | Seconds between posts; raise only if you hit FloodWait |
| `BATCH_LIMIT` | `10000` | Max posts per `/batch` |
| `PROGRESS_INTERVAL` | `6` | Seconds between progress edits |
| `STRING` | – | Premium session string, only for uploads above 2GB |
| `LOG_GROUP` | – | Chat used to stage those >2GB files; the bot must be admin |
| `FORCE_SUB` | `0` | Channel users must join, `0` disables it |
| `BRAND` | `Vsp Official` | Name shown across the bot |
| `JOIN_LINK` | – | Optional updates channel shown on `/start` |
| `DOWNLOAD_DIR` | `downloads` | Temp media directory |
| `PORT` | `8080` | Status page port. Heroku sets this itself — do not override it |

### Tuning

* Fast VPS (1 Gbps+) → `TURBO_STREAMS=24`, `WORKERS=6`
* Heroku or a small box → `TURBO_STREAMS=8`, `WORKERS=2`
* Getting FloodWait → lower `TURBO_STREAMS` first, then set `BATCH_DELAY=2`

Each worker holds one file on disk, so peak temp usage is about
`WORKERS × largest file size`.

---

## Security note

`config.py` used to ship working `API_ID` / `API_HASH` / `BOT_TOKEN` / `MONGO_DB`
/ `MASTER_KEY` values as fallback defaults. They are gone — every credential now
comes from the environment and the bot refuses to start with a clear message if
one is missing.

**Those old values are still in the git history, so rotate them:** a new bot
token from @BotFather, new MongoDB credentials, and fresh `MASTER_KEY` /
`IV_KEY`. Changing the key pair makes stored logins unreadable, so users will
have to `/login` again.

---

Licensed under the GNU General Public License v3.0 — see `LICENSE`.
