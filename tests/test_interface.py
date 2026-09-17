#!/usr/bin/env python3
"""Pasting a link is the whole interaction.

Before this, a link only did something if you had typed /single or /batch first;
paste one on its own and the bot ignored you. Now the link is recognised on its
own and the bot offers what can be done with it, so the commands are a shortcut
rather than a thing you have to know.

Also checks the bot stays quiet during ordinary chatter - a bot that answers
every stray message is worse than one that answers none.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for key, value in {
    'API_ID': '123', 'API_HASH': 'test', 'BOT_TOKEN': '1:test',
    'MONGO_DB': 'mongodb://127.0.0.1:27017', 'OWNER_ID': '1',
    'MASTER_KEY': 'test', 'IV_KEY': 'test',
}.items():
    os.environ.setdefault(key, value)

import plugins.batch as B      # noqa: E402

FAILS = []
STARTED = []


def check(label, got, want=True):
    ok = got == want
    if not ok:
        FAILS.append(label)
    print(('  ok   ' if ok else '  FAIL ') + f'{label}: {got!r}' + ('' if ok else f' (want {want!r})'))


def buttons(markup):
    if not markup:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class Msg:
    def __init__(self, text, uid=5):
        self.text = text
        self.from_user = type('U', (), {'id': uid})()
        self.chat = type('C', (), {'id': uid})()
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kw):
        self.replies.append((text, buttons(reply_markup)))
        return self


class Query:
    def __init__(self, data, uid=5):
        self.data = data
        self.from_user = type('U', (), {'id': uid})()
        self.message = Msg('', uid)
        self.message.edits = []
        self.answers = []

        async def edit_text(text, reply_markup=None, **kw):
            self.message.edits.append((text, buttons(reply_markup)))
        self.message.edit_text = edit_text

    async def answer(self, text=None, **kw):
        self.answers.append(text)


async def fake_start_run(client, message, uid, chat, msg_id, link_type, count):
    STARTED.append((chat, msg_id, link_type, count))


async def main():
    B.start_run = fake_start_run
    B.Z.clear()
    B.ACTIVE.clear()

    print('a pasted link offers what can be done with it')
    m = Msg('https://t.me/c/1234567890/500')
    await B.text_handler(None, m)
    check('bot replied', len(m.replies), 1)
    check('offers both actions', m.replies[0][1],
          ['run_single', 'run_batch', 'run_drop'])
    check('link remembered', B.Z[5]['id'], 500)

    print('\nordinary chatter is ignored')
    for chatter in ('hello', 'thanks!', 'kya haal hai'):
        m = Msg(chatter)
        await B.text_handler(None, m)
        check(f'silent on {chatter!r}', len(m.replies), 0)

    print('\n"this post" runs it straight away')
    STARTED.clear()
    B.Z[5] = {'step': 'chosen', 'chat': '-1001234567890', 'id': 500, 'type': 'private'}
    await B.run_actions(None, Query('run_single'))
    check('started once', len(STARTED), 1)
    check('single post', STARTED[0], ('-1001234567890', 500, 'private', 1))
    check('state cleared', 5 in B.Z, False)

    print('\n"batch from here" asks how many')
    B.Z[5] = {'step': 'chosen', 'chat': '-1001234567890', 'id': 500, 'type': 'private'}
    q = Query('run_batch')
    await B.run_actions(None, q)
    check('offers quick counts', q.message.edits[0][1],
          ['run_n_10', 'run_n_50', 'run_n_100', 'run_n_500', 'run_n_1000',
           'run_n_ask', 'run_drop'])

    print('\npicking a count starts the run')
    STARTED.clear()
    await B.run_actions(None, Query('run_n_100'))
    check('started with 100', STARTED, [('-1001234567890', 500, 'private', 100)])

    print('\na typed count works too')
    STARTED.clear()
    B.Z[5] = {'step': 'awaiting_count', 'chat': '-100777', 'id': 9, 'type': 'private'}
    await B.text_handler(None, Msg('250'))
    check('started with 250', STARTED, [('-100777', 9, 'private', 250)])

    B.Z[5] = {'step': 'awaiting_count', 'chat': '-100777', 'id': 9, 'type': 'private'}
    m = Msg('lots')
    await B.text_handler(None, m)
    check('rejects non-numbers', 'not a number' in m.replies[0][0], True)

    B.Z[5] = {'step': 'awaiting_count', 'chat': '-100777', 'id': 9, 'type': 'private'}
    m = Msg(str(B.BATCH_LIMIT + 1))
    await B.text_handler(None, m)
    check('rejects over the limit', 'between 1 and' in m.replies[0][0], True)

    print('\na stale button says so instead of failing')
    B.Z.clear()
    q = Query('run_single')
    await B.run_actions(None, q)
    check('told it expired', 'expired' in (q.answers[0] or ''), True)

    print('\nthe stop button works with nothing running')
    q = Query('run_stop')
    await B.run_actions(None, q)
    check('says nothing is running', q.answers[0], 'Nothing is running')

    B.ACTIVE[5] = {'cancel': False}
    q = Query('run_stop')
    await B.run_actions(None, q)
    check('asks the run to stop', B.ACTIVE[5]['cancel'], True)
    B.ACTIVE.clear()

    print('\nan invalid link is only called out when one was asked for')
    B.Z[5] = {'step': 'awaiting_link', 'mode': 'batch'}
    m = Msg('not a link at all')
    await B.text_handler(None, m)
    check('explains the format', 'does not look like a post link' in m.replies[0][0], True)

    print('\n' + ('ALL PASS' if not FAILS else f'FAILURES: {FAILS}'))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
