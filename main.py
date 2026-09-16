# Copyright (c) 2025 Vsp Official
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

import asyncio
import importlib
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import uvloop  # a drop-in event loop that is noticeably faster than asyncio's
    uvloop.install()
    print('uvloop enabled 🚀')
except Exception:
    pass

# Imported only after the loop policy is set: pyrogram clients latch onto
# whatever loop exists when they are constructed.
from config import BRAND, DOWNLOAD_DIR, PORT      # noqa: E402
from shared_client import app, start_client        # noqa: E402


class Health(BaseHTTPRequestHandler):
    """Keep-alive endpoint for Koyeb / Render / Heroku web dynos."""

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(f'{BRAND} - restricted content forwarder is running.'.encode())

    def log_message(self, *args):
        pass


def start_health_server():
    """Bind the port before anything slow happens.

    A Heroku web dyno is killed with R10 if it has not bound $PORT within 60
    seconds, and starting the Telegram clients can take longer than that, so
    this has to come first.
    """
    try:
        server = ThreadingHTTPServer(('0.0.0.0', PORT), Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f'Health server listening on :{PORT}')
    except Exception as e:
        if os.getenv('DYNO'):       # on Heroku an unbound port is fatal anyway
            sys.exit(
                f'Could not bind $PORT ({PORT}): {e}\n'
                'A Heroku web dyno must bind the port it is given, so it would '
                'be killed with R10 shortly. Restart the dyno.'
            )
        print(f'Health server not started ({e}), continuing without it.')


async def load_and_run_plugins():
    await start_client()
    for entry in sorted(os.listdir('plugins')):
        if not entry.endswith('.py') or entry == '__init__.py':
            continue
        name = entry[:-3]
        module = importlib.import_module(f'plugins.{name}')
        runner = getattr(module, f'run_{name}_plugin', None)
        if runner:
            print(f'Running {name} plugin...')
            await runner()

    # add_handler registers asynchronously, so let those tasks land before we
    # report. A count of zero means the bot is connected but deaf, which is
    # otherwise invisible - it just silently ignores everything.
    await asyncio.sleep(0.5)
    registered = sum(len(handlers) for handlers in app.dispatcher.groups.values())
    print(f'Handlers registered: {registered}')
    if registered < 5:
        print(
            'WARNING: almost no handlers registered, the bot will not answer. '
            'This means the client is bound to the wrong event loop.'
        )


async def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    start_health_server()
    await load_and_run_plugins()
    print(f'{BRAND} is up. Waiting for links...')
    try:
        await asyncio.Event().wait()
    finally:
        from utils.turbo import close_pools
        await close_pools()


if __name__ == '__main__':
    print('Starting clients ...')
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('Shutting down...')
    except Exception:
        # print(e) alone is close to useless here: a failed Telegram handshake
        # raises KeyError(0), which prints as a bare "0" and tells nobody
        # anything. The traceback is what makes a bad token or a blocked
        # connection diagnosable from `heroku logs`.
        print('Startup failed:', file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
