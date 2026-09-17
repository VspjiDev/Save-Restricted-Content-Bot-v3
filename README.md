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
flight, so throughput scales close to linearly with `TURBO_STREAMS` — **until
Telegram's per-account throttle is reached**, which it usually is. Past that
point more streams do nothing, so the engine attacks the problem from three
other directions:

**1. Move nothing at all.** A post that is not protected is copied server side,
which is instant no matter how big it is. The bot does this for public chats it
can read; your logged-in account does it for private channels, which are often
merely private rather than protected. Only genuinely protected content has to be
transferred, and this needs a target channel set in `/settings`.

**2. Download and upload at the same time.** Telegram throttles each direction
separately, so transferring a file completely and only then sending it wastes
half the available capacity. Each 1 MB downloaded chunk is exactly two upload
parts, and parts may be sent in any order, so a part goes out the moment it
arrives. With each direction capped at 6 MB/s, a 120 MB file takes 40s one after
the other and **20s together** — the same cap, half the wall clock.

**3. Upload several files at once.** Posts have to *arrive* in source order, but
only the message creation needs to be ordered — the bytes can go up whenever. So
files are uploaded during the parallel stage and the ordered stage just attaches
the waiting handle. On a batch of 20 PDFs taking 0.8s to download and 1.2s to
upload each, that is 24.8s with serial uploads and **10.0s at `WORKERS=4`**,
same order out the other end. `WORKERS` is the lever here, and it scales close
to linearly — 24 posts at 0.6s down / 0.9s up each:

| `WORKERS` | 1 | 2 | 4 | 6 | 8 | 12 |
|---|---|---|---|---|---|---|
| time | 36.1s | 18.0s | 9.0s | 6.0s | 4.5s | 3.0s |

RAM does not scale with it: `TRANSFER_MEMORY_MB` caps the file data held in
memory across every transfer, so raising `WORKERS` makes transfers share that
budget rather than each taking their own. It is sized from the container's own
memory limit — 179 MB on a 512 MB dyno, 358 MB on a 1 GB one — so moving to a
bigger dyno takes effect without changing anything. The real ceiling is **disk** — each
worker holds one whole file, so 8–12 is fine for PDFs and clips while multi-GB
videos want 2–3.

**4. Skip work that Telegram already did.** A video's duration and dimensions
come from the source message instead of ffprobe, and its thumbnail is reused
instead of ffmpeg seeking and decoding a frame out of a multi-GB file.

Stream scaling itself, measured against a simulated 200 ms link with no
throttle:

| Streams | Throughput |
|---:|---:|
| 1 (stock pyrogram) | ~5 MB/s |
| 8 | ~40 MB/s |
| **16 (default)** | **~75 MB/s** |
| 24 | ~110 MB/s |

Real throughput is whichever comes first: your host's bandwidth, or Telegram's
per-account limit. If `/status` shows turbo live and speeds are still in single
digits, you are throttled — raising `TURBO_STREAMS` will not help, but the three
mechanisms above still do.

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

* **Keep the dyno count at 1.** Scaling `web` above 1 runs several copies of the
  bot on the same token: every command gets answered once per copy and they
  throttle each other into FloodWait. To run more transfers at once raise the
  `WORKERS` **config var** instead — it is not the dyno count. Extra copies now
  detect each other and stay idle rather than duplicating replies, but they
  still cost money, so scale back to 1.
* **Eco dynos sleep** after 30 minutes without a web request, and a sleeping bot
  answers nothing. Either use a Basic dyno (no sleeping) or point an uptime
  pinger at your app URL.
* **Heroku restarts every dyno about once a day.** A batch running at that
  moment stops where it is; just run it again.
* **The disk is small and temporary** (~1 GB, wiped on restart). Each worker
  holds one whole file, so drop `WORKERS` to `2`–`3` if you mostly move multi-GB
  videos; for ordinary PDFs and clips `8` is a much better trade. RAM is capped
  separately by `TRANSFER_MEMORY_MB`, so it is disk you have to think about. Custom thumbnails are also lost on restart — logins are
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
| `WORKERS` | `6` | Posts transferred at once — downloads *and* uploads. The main speed lever |
| `TRANSFER_MEMORY_MB` | auto | Ceiling on file data held in RAM across all transfers. Sized from the dyno (~35% of it) unless you set it |
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
* Throttled by Telegram (single-digit MB/s with turbo live) → raise `WORKERS`, not `TURBO_STREAMS`
* Mostly multi-GB videos on a small disk → `WORKERS=2`
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
