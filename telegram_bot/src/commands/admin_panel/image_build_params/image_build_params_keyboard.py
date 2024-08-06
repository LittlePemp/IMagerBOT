from aiogram.utils.keyboard import InlineKeyboardBuilder

def image_build_params_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text='Размер изображения', callback_data='param-image_size')
    builder.button(text='Уровень шума', callback_data='param-noise_level')
    builder.button(text='Размер вставки', callback_data='param-inset_size')
    builder.button(text='Назад', callback_data='admin_panel')
    builder.adjust(2)
    return builder.as_markup()
