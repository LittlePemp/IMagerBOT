from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.commands.common_keyboards import add_home_button
from src.models.image_build_params import CommonParam
from src.models.image_group import ImageGroup


def generation_mode_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Выбрать готовые параметры", callback_data="generation_mode:presets")
    builder.button(text="Ввести параметры вручную", callback_data="generation_mode:manual")
    builder.button(text="Назад", callback_data="back")
    add_home_button(builder)
    builder.adjust(1)
    return builder


def image_size_keyboard(sizes: list[CommonParam]) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for size in sizes:
        builder.button(text=f"{size.name} - {size.value}", callback_data=f"image_size_{size.value}")
    builder.button(text="Назад", callback_data="back")
    add_home_button(builder)
    builder.adjust(1)
    return builder


def noise_level_keyboard(noises: list[CommonParam]) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for noise in noises:
        builder.button(text=f"{noise.name} - {noise.value}", callback_data=f"noise_level_{noise.value}")
    builder.button(text="Назад", callback_data="back")
    add_home_button(builder)
    builder.adjust(1)
    return builder


def inset_size_keyboard(insets: list[CommonParam]) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for inset in insets:
        builder.button(text=f"{inset.name} - {inset.value}", callback_data=f"inset_size_{inset.value}")
    builder.button(text="Назад", callback_data="back")
    add_home_button(builder)
    builder.adjust(1)
    return builder


def image_group_keyboard(groups: list[ImageGroup]) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for group in groups:
        builder.button(text=group.display_name, callback_data=f"image_group/{group.imager_name}")
    builder.button(text="Назад", callback_data="back")
    add_home_button(builder)
    builder.adjust(1)
    return builder


def upload_image_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="back")
    add_home_button(builder)
    builder.adjust(1)
    return builder
