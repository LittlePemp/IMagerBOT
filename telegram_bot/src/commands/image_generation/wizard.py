"""Conversation transitions; preset values are resolved server-side."""
from uuid import uuid4

from aiogram.utils.keyboard import InlineKeyboardBuilder

from imager_config.loader import config
from src.application.generation import validate_output
from src.application.generation_presets import (
    list_generation_profiles, load_profile_button_labels, load_profile_defaults,
)
from src.commands.common_keyboards import home_keyboard
from src.models.image_build_params import CommonParam, ParamType
from src.utils.navigation import render_view
from .image_generation_states import ImageGenerationStates as S

STEPS = (
    (ParamType.IMAGE_SIZE, 'image_size', S.selecting_image_size, S.waiting_manual_image_size, 'Качество'),
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


async def start(event, state, prompt='Выберите способ ввода параметров:'):
    await state.clear()
    await state.set_state(S.choosing_generation_mode)
    await render_view(event, state, prompt, keyboard([
        ('Пресеты (реком.)', 'mode:presets'), ('Вручную', 'mode:manual'),
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
    values = await list_generation_profiles(uow.image_build_params_repository)
    labels = load_profile_button_labels()
    page = max(0, min(page, max(0, (len(values) - 1) // config.bot.ui_page_size)))
    start_index = page * config.bot.ui_page_size
    buttons = [(labels.get(item['name'], item['name']), f'pick:{index}')
               for index, item in enumerate(values) if start_index <= index < start_index + config.bot.ui_page_size]
    if page:
        buttons.append(('← Предыдущая', f'preset_page:0:{page - 1}'))
    if start_index + config.bot.ui_page_size < len(values):
        buttons.append(('Следующая →', f'preset_page:0:{page + 1}'))
    buttons.append(('Назад', 'back'))
    await state.update_data(preset_step=0, current_presets={
        str(index): value for index, value in enumerate(values)
        if start_index <= index < start_index + config.bot.ui_page_size
    })
    await state.set_state(S.selecting_image_size)
    await render_view(event, state,
        'Выберите пресет:' if values else 'Пока нет доступных пресетов.',
        keyboard(buttons))


async def preset_page(callback, state, uow):
    _, step, page = callback.data.split(':')
    await show_presets(callback, state, uow, int(step), int(page))
    await callback.answer()


async def pick_preset(callback, state, uow):
    data = await state.get_data()
    selected = data.get('current_presets', {}).get(callback.data.split(':')[1])
    current = await list_generation_profiles(uow.image_build_params_repository)
    if selected is None or selected not in current:
        await callback.answer('Пресет изменён. Обновите выбор.')
        await show_presets(callback, state, uow, 0)
        return
    choices = selected['parameters']
    values = {field: choices[kind.value]['value'] for kind, field, *_ in STEPS}
    try:
        validate_output(values['image_size'], values['inset_size'])
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        await show_presets(callback, state, uow, 0)
        return
    await state.update_data(**values, selected_presets=choices)
    await show_groups(callback, state, uow)
    await callback.answer()


async def show_manual(event, state, uow, step):
    kind, field, _, fsm, title = STEPS[step]
    defaults = (config.generation.result_size, config.generation.noise_level, config.generation.cell_size)
    recommended_name, _ = load_profile_defaults()
    profiles = await list_generation_profiles(uow.image_build_params_repository)
    recommended = next((profile for profile in profiles if profile['name'] == recommended_name), None)
    default = recommended['parameters'][kind.value]['value'] if recommended else defaults[step]
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
        f"Качество: {data['image_size']}; шум: {data['noise_level']}; ячейка: {data['inset_size']}.\n"
        f'Библиотека: {group.display_name}.\nОтправьте фото или стикер Telegram.\n'
        'Для анимированных и видеостикеров используется превью.', keyboard([('Назад', 'back')]))
    await callback.answer()


async def back(callback, state, uow):
    current, data = await state.get_state(), await state.get_data()
    if current == S.waiting_for_image_upload.state:
        await show_groups(callback, state, uow)
    elif current == S.selecting_image_group.state:
        await (show_manual(callback, state, uow, 2) if data.get('generation_mode') == 'manual'
               else show_presets(callback, state, uow, 0))
    else:
        step = next((index for index, item in enumerate(STEPS) if current in (item[2].state, item[3].state)), 0)
        if not step:
            await start(callback, state)
        elif data.get('generation_mode') == 'manual':
            await show_manual(callback, state, uow, step - 1)
        else:
            await show_presets(callback, state, uow, step - 1)
    await callback.answer()
