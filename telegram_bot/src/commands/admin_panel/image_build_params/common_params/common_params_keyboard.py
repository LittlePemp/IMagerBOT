from aiogram.utils.keyboard import InlineKeyboardBuilder

def common_parameter_keyboard(param_type: str):
    builder = InlineKeyboardBuilder()
    builder.button(text='Добавить', callback_data=f'{param_type}-add')
    builder.button(text='Удалить', callback_data=f'{param_type}-delete')
    builder.button(text='Назад', callback_data='admin_panel:image_params')
    builder.adjust(1)
    return builder.as_markup()

def common_back_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text='Назад', callback_data='admin_panel:image_params')
    return builder.as_markup()

def common_delete_keyboard(params, param_type):
    builder = InlineKeyboardBuilder()
    for param in params:
        builder.button(text=f'{param.name}: {param.value}', callback_data=f'delete-{param_type}-{param.name}')
    builder.button(text='Назад', callback_data='admin_panel:image_params')
    return builder.as_markup()
