#!/usr/bin/env python3
"""Startup regression tests.

Run with:  python3 tests/test_startup.py

The bug this exists for: pyrogram's Client and Dispatcher both capture
asyncio.get_event_loop() when they are constructed. shared_client builds the bot
at import time, when no loop is running, so it latched onto a throwaway loop.
asyncio.run() then created a different one, add_handler scheduled its work on the
dead loop, and none of it ever ran. The bot connected, Heroku reported the dyno
up, and every single message was silently ignored.

It only showed up in production because a test that imports the plugins from
inside a running loop gets the right loop by accident. So this test imports
shared_client at module level, exactly the way main.py does.
"""
import asyncio
import importlib
import os
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.simplefilter('ignore')

os.environ.setdefault('API_ID', '123')
os.environ.setdefault('API_HASH', 'test')
os.environ.setdefault('BOT_TOKEN', '1:test')
os.environ.setdefault('MONGO_DB', 'mongodb://127.0.0.1:27017')
os.environ.setdefault('OWNER_ID', '1')
os.environ.setdefault('MASTER_KEY', 'test')
os.environ.setdefault('IV_KEY', 'test')

# Deliberately at module level, before any loop exists - this is the whole point.
from shared_client import app, bind_loop     # noqa: E402

PLUGINS = ['batch', 'login', 'settings', 'start']

FAILS = []


def check(label, got, want=True):
    ok = got == want
    if not ok:
        FAILS.append(label)
    print(('  ok   ' if ok else '  FAIL ') + f'{label}: {got!r}' + ('' if ok else f' (want {want!r})'))


async def main():
    running = asyncio.get_running_loop()

    print('event loop binding')
    check('client built outside the loop is rebound',
          (bind_loop(app), app.dispatcher.loop is running)[1])
    check('client.loop rebound too', app.loop is running)

    print('\nhandler registration')
    baseline = sum(len(g) for g in app.dispatcher.groups.values())
    for name in PLUGINS:
        importlib.import_module(f'plugins.{name}')
    await asyncio.sleep(0.5)          # add_handler lands asynchronously

    handlers = [h for group in app.dispatcher.groups.values() for h in group]
    check('handlers actually registered', len(handlers) > 15, True)

    # Importing the plugins must be what adds them, not a coincidence.
    check('plugins contributed the handlers', len(handlers) - baseline >= 15, True)

    print('\n' + ('ALL PASS' if not FAILS else f'FAILURES: {FAILS}'))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
