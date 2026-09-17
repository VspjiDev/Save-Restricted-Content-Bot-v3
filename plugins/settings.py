# Copyright (c) 2025 Vsp Official
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

import os
import re

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import BRAND
from shared_client import app
from utils.custom_filters import settings_in_progress, set_settings_step, get_settings_step
from utils.safe import safe
from utils.func import (
    VIDEO_EXTENSIONS,
    get_user_data,
    get_user_data_key,
    invalidate_user_cache,
    save_user_data,
    users_collection,
)

def short(value, limit=28):
    text = str(value).replace('\n', ' ').strip()
    return (text[:limit] + '…') if len(text) > limit else text


async def settings_text(user_id):
    """Show what each setting is currently set to, not just its name."""
    data = await get_user_data(user_id, cached=False) or {}
    words = data.get('delete_words') or []
    swaps = data.get('replacement_words') or {}
    lines = [
        f"📝 **Target chat** — {('`' + short(data['chat_id']) + '`') if data.get('chat_id') else '_this chat_'}",
        f"🏷️ **Rename tag** — {short(data['rename_tag']) if data.get('rename_tag') else '_none_'}",
        f"📋 **Caption** — {short(data['caption']) if data.get('caption') else '_original kept_'}",
        f"🔄 **Replacements** — {len(swaps) or '_none_'}",
        f"🗑️ **Removed words** — {len(words) or '_none_'}",
        f"🖼️ **Thumbnail** — {'set' if os.path.exists(f'{user_id}.jpg') else '_none_'}",
    ]
    return f"⚙️ **{BRAND} — Settings**\n\n" + '\n'.join(lines) + '\n\nTap anything to change it.'


MESS = f"⚙️ **{BRAND} — Settings**"

PROMPTS = {
    'setchat': (
        "Send me the target chat ID (with the `-100` prefix):\n\n"
        "👉 __This bot must be an admin in that chat.__\n"
        "👉 __For a topic group use `-100CHANNELID/TOPIC_ID`, e.g. `-1004783898/12`__"
    ),
    'setrename': 'Send me the rename tag:',
    'setcaption': 'Send me the caption:',
    'setreplacement': "Send the replacement words in the format: 'WORD(s)' 'REPLACEWORD'",
    'deleteword': 'Send words separated by space to delete them from captions/filenames:',
    'setthumb': 'Send me the photo you want to use as thumbnail.',
}


def settings_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton('📝 Set Chat ID', callback_data='st_setchat'),
            InlineKeyboardButton('🏷️ Set Rename Tag', callback_data='st_setrename'),
        ],
        [
            InlineKeyboardButton('📋 Set Caption', callback_data='st_setcaption'),
            InlineKeyboardButton('🔄 Replace Words', callback_data='st_setreplacement'),
        ],
        [
            InlineKeyboardButton('🗑️ Remove Words', callback_data='st_deleteword'),
            InlineKeyboardButton('♻️ Reset Settings', callback_data='st_reset'),
        ],
        [
            InlineKeyboardButton('🚪 Logout', callback_data='st_logout'),
        ],
        [
            InlineKeyboardButton('🖼️ Set Thumbnail', callback_data='st_setthumb'),
            InlineKeyboardButton('❌ Remove Thumbnail', callback_data='st_remthumb'),
        ],
    ])


@app.on_message(filters.command('settings') & filters.private)
@safe
async def settings_command(client, message):
    await message.reply_text(await settings_text(message.from_user.id),
                             reply_markup=settings_keyboard())


@app.on_callback_query(filters.regex('^open_settings$'))
@safe
async def open_settings(client, query):
    await query.message.reply_text(await settings_text(query.from_user.id),
                                   reply_markup=settings_keyboard())
    await query.answer()


@app.on_callback_query(filters.regex(r'^st_'))
@safe
async def settings_callback(client, query):
    user_id = query.from_user.id
    action = query.data.split('_', 1)[1]

    if action in PROMPTS:
        msg = await query.message.reply_text(
            f'{PROMPTS[action]}\n\n(Send /cancel to abort)'
        )
        set_settings_step(user_id, {'type': action, 'message_id': msg.id})
        await query.answer()
        return

    if action == 'logout':
        result = await users_collection.update_one(
            {'user_id': user_id}, {'$unset': {'session_string': ''}}
        )
        invalidate_user_cache(user_id)
        await query.message.reply_text(
            'Logged out and deleted session successfully.'
            if result.modified_count else 'You are not logged in.'
        )

    elif action == 'reset':
        try:
            await users_collection.update_one(
                {'user_id': user_id},
                {'$unset': {
                    'delete_words': '', 'replacement_words': '',
                    'rename_tag': '', 'caption': '', 'chat_id': '',
                }},
            )
            invalidate_user_cache(user_id)
            if os.path.exists(f'{user_id}.jpg'):
                os.remove(f'{user_id}.jpg')
            await query.message.reply_text('✅ Settings reset.')
            await query.message.reply_text(await settings_text(user_id),
                                           reply_markup=settings_keyboard())
        except Exception as e:
            await query.message.reply_text(f'❌ Error resetting settings: {e}')

    elif action == 'remthumb':
        try:
            os.remove(f'{user_id}.jpg')
            await query.message.reply_text('✅ Thumbnail removed successfully!')
        except FileNotFoundError:
            await query.message.reply_text('No thumbnail found to remove.')

    await query.answer()


@app.on_message(filters.command('cancel') & filters.private & settings_in_progress)
@safe
async def cancel_settings(client, message):
    set_settings_step(message.from_user.id, None)
    await message.reply_text('✅ Cancelled.')


@app.on_message(settings_in_progress & filters.private & (filters.text | filters.photo))
@safe
async def settings_input(client, message):
    user_id = message.from_user.id
    step = get_settings_step(user_id)
    if not step:
        return
    if message.text and message.text.startswith('/'):
        set_settings_step(user_id, None)
        return

    handlers = {
        'setchat': handle_setchat,
        'setrename': handle_setrename,
        'setcaption': handle_setcaption,
        'setreplacement': handle_setreplacement,
        'deleteword': handle_deleteword,
        'setthumb': handle_setthumb,
    }
    handler = handlers.get(step['type'])
    if handler:
        try:
            await handler(message, user_id)
            await message.reply_text(await settings_text(user_id),
                                     reply_markup=settings_keyboard())
        except Exception as e:
            await message.reply_text(f'❌ Could not save that: {e}')
    set_settings_step(user_id, None)


async def handle_setchat(message, user_id):
    await save_user_data(user_id, 'chat_id', message.text.strip())
    await message.reply_text('✅ Chat ID set successfully!')


async def handle_setrename(message, user_id):
    tag = message.text.strip()
    await save_user_data(user_id, 'rename_tag', tag)
    await message.reply_text(f'✅ Rename tag set to: {tag}')


async def handle_setcaption(message, user_id):
    await save_user_data(user_id, 'caption', message.text)
    await message.reply_text('✅ Caption set successfully!')


async def handle_setreplacement(message, user_id):
    match = re.match(r"'(.+)' '(.+)'", message.text)
    if not match:
        await message.reply_text("❌ Invalid format. Usage: 'WORD(s)' 'REPLACEWORD'")
        return
    word, replace_word = match.groups()
    delete_words = await get_user_data_key(user_id, 'delete_words', []) or []
    if word in delete_words:
        await message.reply_text(f"❌ '{word}' is in the delete list and cannot be replaced.")
        return
    replacements = await get_user_data_key(user_id, 'replacement_words', {}) or {}
    replacements[word] = replace_word
    await save_user_data(user_id, 'replacement_words', replacements)
    await message.reply_text(f"✅ Saved: '{word}' will be replaced with '{replace_word}'")


async def handle_deleteword(message, user_id):
    words = message.text.split()
    delete_words = await get_user_data_key(user_id, 'delete_words', []) or []
    await save_user_data(user_id, 'delete_words', list(set(delete_words + words)))
    await message.reply_text(f"✅ Words added to delete list: {', '.join(words)}")


async def handle_setthumb(message, user_id):
    if not message.photo:
        await message.reply_text('❌ Please send a photo. Operation cancelled.')
        return
    temp_path = await message.download()
    thumb_path = f'{user_id}.jpg'
    if os.path.exists(thumb_path):
        os.remove(thumb_path)
    os.replace(temp_path, thumb_path)
    await message.reply_text('✅ Thumbnail saved successfully!')


def rename_file_sync(file, rename_tag='', delete_words=(), replacements=None):
    """Apply the user's rename rules. Pure string work, no DB round trip."""
    try:
        directory, base = os.path.split(str(file))
        stem, _, ext = base.rpartition('.')
        if not stem:  # no extension at all
            stem, ext = base, 'mp4'
        elif not (ext.isalnum() and len(ext) <= 9):
            stem, ext = base, 'mp4'
        elif ext.lower() in VIDEO_EXTENSIONS:
            ext = 'mp4'

        for word in delete_words or ():
            stem = stem.replace(word, '')
        for word, replacement in (replacements or {}).items():
            stem = stem.replace(word, replacement)

        stem = stem.strip() or 'file'
        new_name = os.path.join(directory, f'{stem} {rename_tag}.{ext}'.replace('  ', ' ').strip())
        if new_name == str(file):
            return file
        os.rename(file, new_name)
        return new_name
    except Exception as e:
        print(f'Rename error: {e}')
        return file


async def rename_file(file, sender, edit=None):
    delete_words = await get_user_data_key(sender, 'delete_words', []) or []
    rename_tag = await get_user_data_key(sender, 'rename_tag', '') or ''
    replacements = await get_user_data_key(sender, 'replacement_words', {}) or {}
    return rename_file_sync(file, rename_tag, delete_words, replacements)
