from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from src.models.image_group import ImageGroup

def image_groups_keyboard(groups):
    keyboard = InlineKeyboardBuilder()
    for group in groups:
        keyboard.button(text=group.display_name, callback_data=f'group/{group.imager_name}')
    keyboard.button(text='Назад', callback_data='admin_panel:image_params')
    return keyboard.as_markup()

def group_action_keyboard(group: ImageGroup):
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text='Активировать' if group.active else 'Деактивировать',
                    callback_data=f'toggle_status/{group.imager_name}')
    keyboard.button(text='Переименовать', callback_data=f'rename/{group.imager_name}')
    keyboard.button(text='Назад', callback_data='admin_panel:image_groups')
    return keyboard.as_markup()

def back_to_image_groups_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text='Назад', callback_data='admin_panel:image_groups')
    return keyboard.as_markup()
