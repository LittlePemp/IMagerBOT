"""A keyboard is a command on one user's current, expiring view."""
import secrets
import time
from datetime import datetime, timezone

from aiogram import BaseMiddleware
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InaccessibleMessage, InlineKeyboardMarkup

from imager_config.loader import config

CURRENT_VIEW = '_current_view'
STALE_VIEW_TEXT = 'Этот экран устарел. Откройте /menu.'


class NavigationCallback(CallbackData, prefix='n'):
    view: str
    item: int


def new_view_token():
    return secrets.token_hex(config.bot.ui_view_token_bytes)


async def bind_view(state, message, *, owner_id, token, records=None):
    expiry = time.time() + config.bot.ui_view_ttl_seconds
    await state.update_data(**{CURRENT_VIEW: {
        'token': token, 'owner_id': owner_id, 'chat_id': message.chat.id,
        'message_id': message.message_id,
        'expires_at': expiry,
        'expires_on': datetime.fromtimestamp(expiry, timezone.utc),
        'records': records or {},
    }})


async def require_current(callback, state, token):
    message = callback.message
    view = (await state.get_data()).get(CURRENT_VIEW, {})
    valid = (
        message is not None and not isinstance(message, InaccessibleMessage)
        and view.get('token') == token
        and view.get('owner_id') == callback.from_user.id
        and view.get('chat_id') == message.chat.id
        and view.get('message_id') == message.message_id
        and view.get('expires_at', 0) > time.time()
    )
    if not valid:
        await callback.answer(STALE_VIEW_TEXT)
    return valid


def _view_keyboard(reply_markup):
    token, actions, rows = new_view_token(), {}, []
    for row in reply_markup.inline_keyboard:
        buttons = []
        for button in row:
            if button.callback_data is None:
                buttons.append(button)
                continue
            item = len(actions)
            actions[str(item)] = button.callback_data
            buttons.append(button.model_copy(update={
                'callback_data': NavigationCallback(view=token, item=item).pack(),
            }))
        rows.append(buttons)
    return token, actions, InlineKeyboardMarkup(inline_keyboard=rows)


async def send_view(bot, state, chat_id, text, reply_markup, *, owner_id):
    """Send a background view with the same owner/message-bound navigation."""
    token, actions, keyboard = _view_keyboard(reply_markup)
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
    await bind_view(state, sent, owner_id=owner_id, token=token, records=actions)
    return sent


async def render_view(event, state, text, reply_markup, *, edit=None, owner_id=None):
    """Store actions on the server; callbacks contain only a nonce and an index."""
    callback = getattr(event, 'message', None) is not None
    message = event.message if callback else event
    if edit is None:
        edit = callback
    owner_id = event.from_user.id if owner_id is None else owner_id
    token, actions, keyboard = _view_keyboard(reply_markup)
    sent = await (message.edit_text(text, reply_markup=keyboard) if edit
                  else message.answer(text, reply_markup=keyboard))
    await bind_view(state, sent, owner_id=owner_id, token=token, records=actions)
    return sent


class NavigationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        raw = event.data or ''
        if raw.startswith('adm:'):
            return await handler(event, data)
        if not raw.startswith('n:'):
            await event.answer(STALE_VIEW_TEXT)
            return
        try:
            command = NavigationCallback.unpack(raw)
        except (ValueError, TypeError):
            await event.answer(STALE_VIEW_TEXT)
            return
        state = data['state']
        if not await require_current(event, state, command.view):
            return
        view = (await state.get_data())[CURRENT_VIEW]
        action = view['records'].get(str(command.item))
        if action is None:
            await event.answer(STALE_VIEW_TEXT)
            return
        event = event.model_copy(update={'data': action})
        data['event_callback_query'] = event
        return await handler(event, data)


class ConversationExpiryMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        state = data['state']
        if not (event.text or '').startswith('/') and await state.get_state():
            view = (await state.get_data()).get(CURRENT_VIEW, {})
            if view.get('expires_at', 0) <= time.time():
                await state.clear()
                await event.answer('Срок ввода истёк. Откройте /menu и начните заново.')
                return
        return await handler(event, data)
