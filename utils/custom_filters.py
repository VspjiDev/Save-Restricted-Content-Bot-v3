# Copyright (c) 2025 devgagan : https://github.com/devgaganin.
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

from pyrogram import filters

# user_id -> step, shared by the /login conversation
user_steps = {}

# user_id -> {'type': ..., 'message_id': ...}, shared by the /settings conversation
settings_steps = {}


def _login_filter(_, __, message):
    return bool(message.from_user) and message.from_user.id in user_steps


def _settings_filter(_, __, message):
    return bool(message.from_user) and message.from_user.id in settings_steps


# Pyrogram only runs the first matching handler per group, so the text handlers in
# login.py / settings.py / batch.py must stay mutually exclusive.
login_in_progress = filters.create(_login_filter)
settings_in_progress = filters.create(_settings_filter)


def set_user_step(user_id, step=None):
    if step:
        user_steps[user_id] = step
    else:
        user_steps.pop(user_id, None)


def get_user_step(user_id):
    return user_steps.get(user_id)


def set_settings_step(user_id, step=None):
    if step:
        settings_steps[user_id] = step
    else:
        settings_steps.pop(user_id, None)


def get_settings_step(user_id):
    return settings_steps.get(user_id)
