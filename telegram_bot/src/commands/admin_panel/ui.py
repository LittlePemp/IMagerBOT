"""Telegram views with short callbacks bound to one owner and screen revision."""
from dataclasses import dataclass

from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from imager_config.loader import config
from settings import settings
from src.commands.common_keyboards import HOME_CALLBACK
from src.utils.navigation import NavigationCallback, bind_view, new_view_token, require_current


HOME_ACTION_INDEX = -1
RECORD_ACTIONS = frozenset({
    'grant', 'revoke', 'ban', 'unban', 'preset_remove', 'group_show',
    'group_enable', 'group_disable', 'group_rename',
})


async def authorize_change(event, user_repository):
    if event.from_user.id in settings.telegram_admin_ids:
        return
    actor = await user_repository.get_user_by_id(event.from_user.id)
    if actor is None or not actor.is_admin or actor.isbanned:
        raise ValueError('Недостаточно прав. Вернитесь в главное меню.')


class AdminCallback(CallbackData, prefix='adm'):
    view: str
    action: str
    item: int = 0


@dataclass(frozen=True)
class AdminCommand:
    action: str
    item: int
    record: dict
    context: dict


def label(text):
    return str(text)[:config.bot.ui_label_max_length]


def page_slice(items, page):
    last = max(0, (len(items) - 1) // config.bot.ui_page_size)
    page = min(max(page, 0), last)
    offset = page * config.bot.ui_page_size
    return page, last, list(enumerate(items[offset:offset + config.bot.ui_page_size], offset))


def page_buttons(page, last, action):
    buttons = []
    if page:
        buttons.append(('← Предыдущая', action, page - 1))
    if page < last:
        buttons.append(('Следующая →', action, page + 1))
    return buttons


async def show(event, state, text, buttons, *, records=None, context=None, fsm=None):
    token = new_view_token()
    builder = InlineKeyboardBuilder()
    for title, action, item in buttons:
        builder.button(text=label(title), callback_data=AdminCallback(view=token, action=action, item=item))
    builder.button(text='Главное меню', callback_data=NavigationCallback(view=token, item=HOME_ACTION_INDEX))
    builder.adjust(1)
    if isinstance(event, CallbackQuery):
        message = await event.message.edit_text(text, reply_markup=builder.as_markup())
    else:
        message = await event.answer(text, reply_markup=builder.as_markup())
    await state.set_state(fsm)
    await state.update_data(admin_context=context or {})
    view_records = dict(records or {})
    view_records[str(HOME_ACTION_INDEX)] = HOME_CALLBACK
    await bind_view(state, message, owner_id=event.from_user.id, token=token, records=view_records)
    return message


async def command(callback, state):
    data = AdminCallback.unpack(callback.data)
    if not await require_current(callback, state, data.view):
        return None
    snapshot = await state.get_data()
    view = snapshot['_current_view']
    record = view.get('records', {}).get(str(data.item), {})
    if data.action in RECORD_ACTIONS and (not isinstance(record, dict) or not record):
        await callback.answer('Этот пункт больше недоступен. Откройте список заново.')
        return None
    await callback.answer()
    return AdminCommand(data.action, data.item, record if isinstance(record, dict) else {}, snapshot.get('admin_context', {}))
