from functools import partial

from aiogram import Dispatcher, F

from src.infrastructure.services.imager_service import imager_service
from src.models.image_build_params import validate_display_name
from src.models.image_group import GroupStatus
from src.utils.filters.request_filters.admin_filter import AdminFilter
from ...ui import AdminCallback, authorize_change, command, page_buttons, page_slice, show
from .image_groups_states import ImageGroupsStates


async def render_groups(event, state, uow, page=0):
    result = await uow.image_group_repository.get_groups(status=None)
    page, last, visible = page_slice(result.value, page)
    records = {str(index): {'imager_name': group.imager_name} for index, group in visible}
    buttons = [
        (f"{'✓' if group.active else '—'} {group.display_name}", 'group_show', index)
        for index, group in visible
    ]
    buttons.extend(page_buttons(page, last, 'groups'))
    buttons.extend([('Обновить список из ядра', 'groups_sync', 0), ('Назад', 'params', 0)])
    await show(event, state,
               f'Библиотеки. Страница {page + 1}/{last + 1}.\n'
               'Показаны включённые и отключённые библиотеки.',
               buttons, records=records, context={'page': page},
               fsm=ImageGroupsStates.selecting_group)


async def show_image_groups_menu(callback, state, uow):
    action = await command(callback, state)
    if action:
        await render_groups(callback, state, uow, action.item)


async def sync_groups(callback, state, uow):
    action = await command(callback, state)
    if action:
        await authorize_change(callback, uow.user_repository)
        response = await imager_service.list_groups()
        await uow.image_group_repository.sync_groups(response.groups)
        await render_groups(callback, state, uow, action.context.get('page', 0))


async def render_group(event, state, uow, name, page=0):
    group = await uow.image_group_repository.get_group_by_name(name)
    if group is None:
        await render_groups(event, state, uow, page)
        return
    record = {'imager_name': name, 'revision': group.revision}
    await show(
        event, state,
        f'Библиотека: {group.display_name}\n'
        f'Изображений: {group.count}\n'
        f'Разрешена администратором: {"да" if group.status == GroupStatus.ACTIVE else "нет"}\n'
        f'Доступна в ядре: {"да" if group.available_in_core else "нет"}',
        [
            ('Отключить' if group.status == GroupStatus.ACTIVE else 'Включить',
             'group_disable' if group.status == GroupStatus.ACTIVE else 'group_enable', 0),
            ('Переименовать', 'group_rename', 0),
            ('Назад', 'groups', page),
        ],
        records={'0': record}, context={'page': page},
        fsm=ImageGroupsStates.viewing_group_details,
    )


async def show_group_details(callback, state, uow):
    action = await command(callback, state)
    if action and action.record:
        await render_group(callback, state, uow, action.record['imager_name'], action.context.get('page', 0))


async def set_group_status(callback, state, uow):
    action = await command(callback, state)
    if not action or not action.record:
        return
    await authorize_change(callback, uow.user_repository)
    try:
        await uow.image_group_repository.update_group_status(
            action.record['imager_name'],
            GroupStatus.ACTIVE if action.action == 'group_enable' else GroupStatus.INACTIVE,
            expected_revision=action.record['revision'],
        )
    except ValueError as error:
        await show(callback, state, str(error), [('Обновить список', 'groups', 0)])
        return
    await render_group(callback, state, uow, action.record['imager_name'], action.context.get('page', 0))


async def rename_group(callback, state):
    action = await command(callback, state)
    if action and action.record:
        await show(callback, state, 'Введите новое отображаемое название:', [
            ('Назад', 'groups', action.context.get('page', 0)),
        ], context=dict(action.context, **action.record), fsm=ImageGroupsStates.renaming_group)


async def receive_new_group_name(message, state, uow):
    context = (await state.get_data())['admin_context']
    try:
        new_name = validate_display_name(message.text)
        await authorize_change(message, uow.user_repository)
        await uow.image_group_repository.update_group_name(
            context['imager_name'], new_name, expected_revision=context['revision'],
        )
    except ValueError as error:
        await message.answer(str(error))
        return
    await render_group(message, state, uow, context['imager_name'], context.get('page', 0))


def register_handlers_image_groups(dp: Dispatcher, uow):
    for handler, actions in (
        (show_image_groups_menu, {'groups'}), (sync_groups, {'groups_sync'}),
        (show_group_details, {'group_show'}), (set_group_status, {'group_enable', 'group_disable'}),
    ):
        dp.callback_query.register(partial(handler, uow=uow), AdminCallback.filter(F.action.in_(actions)), AdminFilter())
    dp.callback_query.register(rename_group, AdminCallback.filter(F.action == 'group_rename'), AdminFilter())
    dp.message.register(partial(receive_new_group_name, uow=uow), ImageGroupsStates.renaming_group, AdminFilter())
