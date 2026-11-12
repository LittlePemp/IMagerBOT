"""Conversation transitions; preset values are resolved server-side."""
from uuid import uuid4

from aiogram.utils.keyboard import InlineKeyboardBuilder

from imager_config.loader import config
from src.application.generation import validate_output
from src.commands.common_keyboards import home_keyboard
from src.models.image_build_params import CommonParam, ParamType
from src.utils.navigation import render_view
from .image_generation_states import ImageGenerationStates as S

STEPS = (
    (ParamType.IMAGE_SIZE, 'image_size', S.selecting_image_size, S.waiting_manual_image_size, 'Размер результата'),
    (ParamType.NOISE_LEVEL, 'noise_level', S.selecting_noise_level, S.waiting_manual_noise_level, 'Уровень шума'),
    (ParamType.INSET_SIZE, 'inset_size', S.selecting_inset_size, S.waiting_manual_inset_size, 'Размер ячейки'),
)


def keyboard(buttons):
    builder = InlineKeyboardBuilder()
    for text, action in buttons:
        builder.button(text=str(text)[:config.bot.ui_label_max_length], callback_data=action)
    builder.button(text='Главное меню', callback_data='main_menu')
    builder.adjust(1)
    return builder.as_markup()


async def start(event, state):
    await state.clear()
    await state.set_state(S.choosing_generation_mode)
    await render_view(event, state, 'Выберите способ ввода параметров:', keyboard([
        ('Из пресетов', 'mode:presets'), ('Вручную', 'mode:manual'),
    ]))


async def choose_mode(callback, state, uow):
    mode = callback.data.split(':')[1]
    await state.clear()
    await state.update_data(draft_id=uuid4().hex, generation_mode=mode, selected_presets={})
    if mode == 'manual':
        await show_manual(callback, state, uow, 0)
    else:
        await show_presets(callback, state, uow, 0)
    await callback.answer()


async def show_presets(event, state, uow, step, page=0):
    kind, _, fsm, _, title = STEPS[step]
    result = await uow.image_build_params_repository.get_common_params(kind)
    values = result.value if result.is_success else []
    page = max(0, min(page, max(0, (len(values) - 1) // config.bot.ui_page_size)))
    start_index = page * config.bot.ui_page_size
    buttons = [(f'{item.name}: {item.value}', f'pick:{index}')
               for index, item in enumerate(values) if start_index <= index < start_index + config.bot.ui_page_size]
    if page:
        buttons.append(('← Предыдущая', f'preset_page:{step}:{page - 1}'))
    if start_index + config.bot.ui_page_size < len(values):
        buttons.append(('Следующая →', f'preset_page:{step}:{page + 1}'))
    buttons.append(('Назад', 'back'))
    await state.update_data(preset_step=step, current_presets={
        str(index): value.model_dump(mode='json') for index, value in enumerate(values)
        if start_index <= index < start_index + config.bot.ui_page_size
    })
    await state.set_state(fsm)
    await render_view(event, state, title if values else 'Для этого шага пока нет допустимых пресетов.', keyboard(buttons))


async def preset_page(callback, state, uow):
    _, step, page = callback.data.split(':')
    await show_presets(callback, state, uow, int(step), int(page))
    await callback.answer()


async def pick_preset(callback, state, uow):
    data = await state.get_data()
    step = data['preset_step']
    selected = CommonParam(**data['current_presets'][callback.data.split(':')[1]])
    current = await uow.image_build_params_repository.get_common_params(selected.type)
    if not current.is_success or not any(item == selected for item in current.value):
        await callback.answer('Пресет изменён. Обновите выбор.')
        await show_presets(callback, state, uow, step)
        return
    choices = dict(data.get('selected_presets', {}))
    choices[selected.type.value] = selected.model_dump(mode='json')
    await state.update_data(**{STEPS[step][1]: selected.value, 'selected_presets': choices})
    if step < len(STEPS) - 1:
        await show_presets(callback, state, uow, step + 1)
    else:
        try:
            validate_output((await state.get_data())['image_size'], selected.value)
        except ValueError as error:
            await callback.answer(str(error), show_alert=True)
            await show_presets(callback, state, uow, step)
            return
        await show_groups(callback, state, uow)
    await callback.answer()


async def show_manual(event, state, uow, step):
    kind, field, _, fsm, title = STEPS[step]
    prefs = await uow.user_repository.get_generation_preferences(event.from_user.id) or {}
    defaults = (config.generation.result_size, config.generation.noise_level, config.generation.cell_size)
    try:
        default = CommonParam(name=title, type=kind, value=prefs.get(field, defaults[step])).value
    except ValueError:
        default = defaults[step]
    await state.set_state(fsm)
    await render_view(event, state, f'{title}: введите целое число. Рекомендуемое значение: {default}.', keyboard([('Назад', 'back')]))


async def manual_value(message, state, uow):
    current_state = await state.get_state()
    step = next(index for index, item in enumerate(STEPS) if item[3].state == current_state)
    kind, field, _, _, title = STEPS[step]
    result = CommonParam.create(name=title, value=message.text, type=kind)
    if not result.is_success:
        await render_view(message, state, result.error, keyboard([('Назад', 'back')]))
        return
    data = await state.get_data()
    if step == len(STEPS) - 1:
        try:
            validate_output(data['image_size'], result.value.value)
        except ValueError as error:
            await render_view(message, state, str(error), keyboard([('Назад', 'back')]))
            return
    await state.update_data(**{field: result.value.value})
    if step < len(STEPS) - 1:
        await show_manual(message, state, uow, step + 1)
    else:
        data = await state.get_data()
        await uow.user_repository.update_generation_preferences(message.from_user.id, {
            item[1]: data[item[1]] for item in STEPS
        })
        await show_groups(message, state, uow)


async def show_groups(event, state, uow, page=0):
    result = await uow.image_group_repository.get_groups()
    values = result.value if result.is_success else []
    page = max(0, min(page, max(0, (len(values) - 1) // config.bot.ui_page_size)))
    offset = page * config.bot.ui_page_size
    buttons = [(item.display_name, f'group:{index}') for index, item in enumerate(values)
               if offset <= index < offset + config.bot.ui_page_size]
    if page:
        buttons.append(('← Предыдущая', f'group_page:{page - 1}'))
    if offset + config.bot.ui_page_size < len(values):
        buttons.append(('Следующая →', f'group_page:{page + 1}'))
    buttons.append(('Назад', 'back'))
    await state.update_data(current_groups={
        str(index): item.imager_name for index, item in enumerate(values)
        if offset <= index < offset + config.bot.ui_page_size
    })
    await state.set_state(S.selecting_image_group)
    await render_view(event, state, 'Выберите библиотеку:' if values else 'Нет доступных библиотек.', keyboard(buttons))


async def group_page(callback, state, uow):
    await show_groups(callback, state, uow, int(callback.data.split(':')[1]))
    await callback.answer()


async def choose_group(callback, state, uow):
    data = await state.get_data()
    name = data['current_groups'][callback.data.split(':')[1]]
    group = await uow.image_group_repository.get_group_by_name(name)
    if group is None or not group.active:
        await callback.answer('Библиотека больше недоступна.')
        await show_groups(callback, state, uow)
        return
    await state.update_data(image_group=name)
    await state.set_state(S.waiting_for_image_upload)
    await render_view(callback, state,
        f"Размер: {data['image_size']}; шум: {data['noise_level']}; ячейка: {data['inset_size']}.\n"
        f'Библиотека: {group.display_name}.\nОтправьте изображение как фото.', keyboard([('Назад', 'back')]))
    await callback.answer()


async def back(callback, state, uow):
    current, data = await state.get_state(), await state.get_data()
    if current == S.waiting_for_image_upload.state:
        await show_groups(callback, state, uow)
    elif current == S.selecting_image_group.state:
        await (show_manual(callback, state, uow, 2) if data.get('generation_mode') == 'manual'
               else show_presets(callback, state, uow, 2))
    else:
        step = next((index for index, item in enumerate(STEPS) if current in (item[2].state, item[3].state)), 0)
        if not step:
            await start(callback, state)
        elif data.get('generation_mode') == 'manual':
            await show_manual(callback, state, uow, step - 1)
        else:
            await show_presets(callback, state, uow, step - 1)
    await callback.answer()
