# Copyright (c) 2025 Vsp Official
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

from pyrogram import filters
from pyrogram.errors import UserNotParticipant
from pyrogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    BRAND, FORCE_SUB, JOIN_LINK, OWNER_ID,
    TRANSFER_MEMORY_MB, TURBO_DISABLED, TURBO_STREAMS, WORKERS,
)
from shared_client import app
from utils import turbo
from utils.safe import safe
from utils.func import get_user_data


async def subscribe(client, message):
    """Returns 1 when the user still has to join the force-sub channel."""
    if not FORCE_SUB:
        return 0
    try:
        member = await client.get_chat_member(FORCE_SUB, message.from_user.id)
        if str(member.status) == 'ChatMemberStatus.BANNED':
            await message.reply_text('You are banned from using this bot.')
            return 1
    except UserNotParticipant:
        try:
            link = await client.export_chat_invite_link(FORCE_SUB)
        except Exception:
            link = JOIN_LINK
        if not link:
            return 0
        await message.reply_text(
            'Join our channel to use the bot.',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Join Now', url=link)]]),
        )
        return 1
    except Exception as e:
        await message.reply_text(f'Force-sub check failed, contact the admin: {e}')
        return 1
    return 0


async def start_text(user_id):
    """Intro plus a live setup checklist, so what is missing is obvious."""
    data = await get_user_data(user_id, cached=False) or {}
    logged_in = bool(data.get('session_string'))
    target = data.get('chat_id')

    steps = [
        f"{'✅' if logged_in else '⬜'} **Login** — "
        + ('connected' if logged_in else 'needed for private channels · /login'),
        f"{'✅' if target else '⬜'} **Target chat** — "
        + (f'`{target}`' if target else 'optional · posts come here otherwise'),
    ]
    return (
        f"👋 **{BRAND}**\n\n"
        "I save posts from channels where forwarding is turned off — public and "
        "private both.\n\n"
        "**Just paste a post link.** I will ask what to do with it.\n\n"
        + '\n'.join(steps) +
        "\n\n`https://t.me/channel/123`\n`https://t.me/c/1234567890/123`"
    )

HELP_TEXT = (
    f"📖 **{BRAND} — Help**\n\n"
    "**The short version**\n"
    "Paste a post link. Pick *This post* or *Batch from here*. Done.\n\n"
    "**Link formats**\n"
    "`https://t.me/channel/123` — public\n"
    "`https://t.me/c/1234567890/123` — private\n"
    "`https://t.me/c/1234567890/12/123` — topic\n\n"
    "**Commands**\n"
    "/single · /batch — start without pasting first\n"
    "/stop — stop a run\n"
    "/login · /logout — connect your account for private channels\n"
    "/settings — where posts go and how they look\n"
    "/status — login, target chat and engine\n\n"
    "**Settings worth knowing**\n"
    "• **Target chat** — send straight into your channel instead of this chat. "
    "Also lets unprotected posts be copied instantly, with no transfer at all.\n"
    "• **Caption / Rename tag** — add your own text to every post.\n"
    "• **Replace / Remove words** — clean up the original caption.\n"
    "• **Thumbnail** — one cover image for every video.\n\n"
    "**If a run is slow**\n"
    "Telegram limits transfer speed per account, so protected content moves at "
    "whatever that allows. Unprotected posts skip the transfer entirely — set a "
    "target chat and they arrive instantly."
)


def start_keyboard():
    rows = [[
        InlineKeyboardButton('⚙️ Settings', callback_data='open_settings'),
        InlineKeyboardButton('❓ Help', callback_data='show_help'),
    ]]
    if JOIN_LINK:
        rows.insert(0, [InlineKeyboardButton('📢 Updates', url=JOIN_LINK)])
    return InlineKeyboardMarkup(rows)


@app.on_message(filters.command('start') & filters.private)
@safe
async def start_handler(client, message):
    if await subscribe(client, message) == 1:
        return
    await message.reply_text(await start_text(message.from_user.id),
                             reply_markup=start_keyboard(), disable_web_page_preview=True)


@app.on_message(filters.command('help') & filters.private)
@safe
async def help_handler(client, message):
    if await subscribe(client, message) == 1:
        return
    await message.reply_text(HELP_TEXT, disable_web_page_preview=True)


@app.on_callback_query(filters.regex('^show_help$'))
@safe
async def show_help(client, query):
    await query.message.reply_text(HELP_TEXT, disable_web_page_preview=True)
    await query.answer()


@app.on_message(filters.command('status') & filters.private)
@safe
async def status_handler(client, message):
    data = await get_user_data(message.from_user.id, cached=False) or {}
    engine = turbo.status_line() if not TURBO_DISABLED else 'disabled by config'
    await message.reply_text(
        f'**{BRAND} — your status**\n\n'
        f"**Login:** {'✅ Active' if data.get('session_string') else '❌ Inactive'}\n"
        f"**Target chat:** `{data.get('chat_id') or 'this chat'}`\n"
        f'**Parallel posts:** {WORKERS}  ·  **Streams/file:** {TURBO_STREAMS}\n'
        f'**Memory budget:** {TRANSFER_MEMORY_MB} MB\n'
        f'**Turbo engine:** {engine}'
    )


@app.on_message(filters.command('set') & filters.private)
@safe
async def set_commands(client, message):
    if message.from_user.id not in OWNER_ID:
        await message.reply_text('You are not authorized to use this command.')
        return
    await app.set_bot_commands([
        BotCommand('start', '🚀 Start the bot'),
        BotCommand('single', '⚡ Extract a single post'),
        BotCommand('batch', '🫠 Extract in bulk'),
        BotCommand('stop', '🚫 Cancel the running batch'),
        BotCommand('login', '🔑 Login for private channels'),
        BotCommand('logout', '🚪 Remove your session'),
        BotCommand('settings', '⚙️ Personalize things'),
        BotCommand('status', '📊 Your current status'),
        BotCommand('help', '❓ How to use the bot'),
    ])
    await message.reply_text('✅ Commands configured successfully!')
