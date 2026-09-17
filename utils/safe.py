# Copyright (c) 2025 Vsp Official
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

"""Never let a handler fail silently.

Pyrogram logs an exception from a handler and moves on, so from the outside the
bot simply does not answer - which is the least useful thing it could do. This
turns any unhandled error into a short, honest reply, and keeps the traceback in
the log where it belongs.
"""

import functools
import logging
import traceback

from pyrogram.errors import FloodWait, MessageNotModified

log = logging.getLogger(__name__)


async def _tell(update, text):
    """Answer whichever kind of update this was."""
    try:
        if hasattr(update, 'answer') and hasattr(update, 'data'):      # callback query
            await update.answer(text[:190], show_alert=True)
        elif hasattr(update, 'reply_text'):                            # message
            await update.reply_text(text)
    except Exception:
        pass


def safe(handler):
    @functools.wraps(handler)
    async def wrapper(client, update, *args, **kwargs):
        try:
            return await handler(client, update, *args, **kwargs)
        except MessageNotModified:
            pass
        except FloodWait as e:
            await _tell(update, f'⏳ Telegram asked me to wait {e.value}s. Try again after that.')
        except Exception as e:
            log.error('handler %s failed: %s', handler.__name__, traceback.format_exc())
            await _tell(
                update,
                f'❌ Something went wrong: `{type(e).__name__}: {e}`'[:400]
                + '\n\nIf it keeps happening, send /status and share what it says.',
            )
    return wrapper
