# Copyright (c) 2025 Vsp Official
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

from pyrogram import filters
from pyrogram.errors import UserNotParticipant
from pyrogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

from config import BRAND, FORCE_SUB, JOIN_LINK, OWNER_ID, TURBO_DISABLED, TURBO_STREAMS, WORKERS
from shared_client import app
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


START_TEXT = (
    f"👋 **{BRAND} — Restricted Content Saver**\n\n"
    "⚡ I save posts from channels and groups where forwarding is off — public and private both.\n\n"
    "**How to use**\n"
    "1. `/login` — only needed for private channels\n"
    "2. `/single` — send one post link\n"
    "3. `/batch` — bulk extract\n\n"
    "Send /help for everything else."
)

HELP_TEXT = (
    f"📝 **{BRAND} — Commands**\n\n"
    "**Extract**\n"
    "• `/single` — extract one post (then send the link)\n"
    "• `/batch` — bulk extract (start link + how many)\n"
    "• `/stop` — cancel a running batch\n\n"
    "**Account**\n"
    "• `/login` — log in so private channels can be read\n"
    "• `/logout` — remove your session\n"
    "• `/status` — your current status\n\n"
    "**Settings** — `/settings`\n"
    "• Set Chat ID — upload straight into a channel, group or topic\n"
    "• Set Rename Tag — add your tag to filenames\n"
    "• Set Caption — custom caption\n"
    "• Replace / Remove Words — clean up captions and filenames\n"
    "• Set Thumbnail — custom video thumbnail\n"
    "• Reset — back to defaults\n\n"
    "**Link formats**\n"
    "• Public: `https://t.me/channel/123`\n"
    "• Private: `https://t.me/c/1234567890/123`\n"
    "• Topic: `https://t.me/c/1234567890/12/123`\n\n"
    "**Note**: a public post that is not protected is copied instantly with no "
    "download at all. Everything else is downloaded and re-uploaded."
)


def start_keyboard():
    rows = [[InlineKeyboardButton('❓ Help', callback_data='show_help')]]
    if JOIN_LINK:
        rows.insert(0, [InlineKeyboardButton('📢 Updates', url=JOIN_LINK)])
    return InlineKeyboardMarkup(rows)


@app.on_message(filters.command('start') & filters.private)
async def start_handler(client, message):
    if await subscribe(client, message) == 1:
        return
    await message.reply_text(START_TEXT, reply_markup=start_keyboard(), disable_web_page_preview=True)


@app.on_message(filters.command('help') & filters.private)
async def help_handler(client, message):
    if await subscribe(client, message) == 1:
        return
    await message.reply_text(HELP_TEXT, disable_web_page_preview=True)


@app.on_callback_query(filters.regex('^show_help$'))
async def show_help(client, query):
    await query.message.reply_text(HELP_TEXT, disable_web_page_preview=True)
    await query.answer()


@app.on_message(filters.command('status') & filters.private)
async def status_handler(client, message):
    data = await get_user_data(message.from_user.id, cached=False) or {}
    engine = f'{TURBO_STREAMS} streams/file' if not TURBO_DISABLED else 'off'
    await message.reply_text(
        f'**{BRAND} — your status**\n\n'
        f"**Login:** {'✅ Active' if data.get('session_string') else '❌ Inactive'}\n"
        f"**Target chat:** `{data.get('chat_id') or 'this chat'}`\n"
        f'**Turbo engine:** {engine}\n'
        f'**Parallel files:** {WORKERS}'
    )


@app.on_message(filters.command('set') & filters.private)
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
