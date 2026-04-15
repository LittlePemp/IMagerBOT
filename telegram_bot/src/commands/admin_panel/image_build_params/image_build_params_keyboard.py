from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.commands.common_keyboards import add_home_button


def image_build_params_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Размер изображения", callback_data="param-image_size")
    builder.button(text="Уровень шума", callback_data="param-noise_level")
    builder.button(text="Размер вставки", callback_data="param-inset_size")
    builder.button(text="Группы изображений", callback_data="admin_panel:image_groups")
    builder.button(text="Назад", callback_data="admin_panel")
    add_home_button(builder)
    builder.adjust(2, 2, 1)
    return builder.as_markup()
