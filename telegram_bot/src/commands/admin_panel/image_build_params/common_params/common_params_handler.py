from functools import partial

from aiogram import Dispatcher, F

from src.models.image_build_params import CommonParam, ParamType, validate_display_name
from src.utils.filters.request_filters.admin_filter import AdminFilter
from ...ui import AdminCallback, authorize_change, command, page_buttons, page_slice, show
from .common_params_states import CommonParamsStates


PARAM_TYPES = (ParamType.IMAGE_SIZE, ParamType.NOISE_LEVEL, ParamType.INSET_SIZE)
PARAM_NAMES = ('Размер результата', 'Уровень шума', 'Размер ячейки')


async def render_presets(event, state, uow, type_index, page=0):
    param_type = PARAM_TYPES[type_index]
    rows = await uow.image_build_params_repository.get_admin_params(param_type)
    page, last, visible = page_slice(rows, page)
    records = {str(index): {'record_id': row['record_id']} for index, row in visible}
    buttons = [(f"Удалить: {row.get('name', 'Без названия')} = {row.get('value', '?')}", 'preset_remove', index)
               for index, row in visible]
    buttons.extend(page_buttons(page, last, 'preset_page'))
    buttons.extend([('Добавить', 'preset_add', 0), ('Назад', 'params', 0)])
    await show(
        event, state,
        f'{PARAM_NAMES[type_index]}. Страница {page + 1}/{last + 1}.\n'
        'Выберите значение для удаления или добавьте новое.',
        buttons, records=records, context={'type_index': type_index, 'page': page},
    )


async def show_param_details(callback, state, uow):
    action = await command(callback, state)
    if action and 0 <= action.item < len(PARAM_TYPES):
        await render_presets(callback, state, uow, action.item)


async def change_page(callback, state, uow):
    action = await command(callback, state)
    if action:
        await render_presets(callback, state, uow, action.context['type_index'], action.item)


async def add_param(callback, state):
    action = await command(callback, state)
    if action:
        await show(callback, state, 'Введите название нового варианта:', [
            ('Назад', 'preset_list', action.context['type_index']),
        ], context=action.context, fsm=CommonParamsStates.waiting_for_name)


async def receive_param_name(message, state):
    snapshot = await state.get_data()
    context = snapshot['admin_context']
    try:
        name = validate_display_name(message.text)
    except ValueError as error:
        await message.answer(str(error))
        return
    context = dict(context, param_name=name)
    await show(message, state, 'Введите целое числовое значение:', [
        ('Назад', 'preset_list', context['type_index']),
    ], context=context, fsm=CommonParamsStates.waiting_for_value)


async def receive_param_value(message, state, uow):
    context = (await state.get_data())['admin_context']
    result = CommonParam.create(
        name=context['param_name'], value=message.text,
        type=PARAM_TYPES[context['type_index']],
    )
    if not result.is_success:
        await message.answer(result.error)
        return
    await authorize_change(message, uow.user_repository)
    saved = await uow.image_build_params_repository.add_common_param(result.value)
    if not saved.is_success:
        await message.answer(saved.error)
        return
    await render_presets(message, state, uow, context['type_index'], context.get('page', 0))


async def confirm_delete_param(callback, state, uow):
    action = await command(callback, state)
    if action is None:
        return
    if action.record:
        await authorize_change(callback, uow.user_repository)
        await uow.image_build_params_repository.delete_by_id(
            action.record['record_id'], PARAM_TYPES[action.context['type_index']],
        )
    await render_presets(callback, state, uow, action.context['type_index'], action.context.get('page', 0))


def register_handlers_common_params(dp: Dispatcher, uow):
    for handler, action in (
        (show_param_details, 'preset_list'), (change_page, 'preset_page'),
        (confirm_delete_param, 'preset_remove'),
    ):
        dp.callback_query.register(partial(handler, uow=uow), AdminCallback.filter(F.action == action), AdminFilter())
    dp.callback_query.register(add_param, AdminCallback.filter(F.action == 'preset_add'), AdminFilter())
    dp.message.register(receive_param_name, CommonParamsStates.waiting_for_name, AdminFilter())
    dp.message.register(partial(receive_param_value, uow=uow), CommonParamsStates.waiting_for_value, AdminFilter())
