from functools import partial

from aiogram import Dispatcher, types
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from src.infrastructure.data.unit_of_work import MongoUnitOfWork
from src.infrastructure.services.imager_service import imager_service
from src.models.image_group import GroupStatus
from src.utils.loggers import exception_logger

from .image_groups_keyboard import (back_to_image_groups_keyboard,
                                    group_action_keyboard,
                                    image_groups_keyboard)
from .image_groups_states import ImageGroupsStates


async def show_image_groups_menu(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    await state.clear()

    groups_response_result = await imager_service.list_groups()
    if not groups_response_result:
        exception_logger.error(f'Failed to retrieve image groups: {groups_response_result.error}')
        await callback.message.edit_text('Ошибка при получении списка групп.')
        await callback.answer()
        return

    groups_response = groups_response_result.value

    sync_result = await uow.image_group_repository.sync_groups(groups_response.groups)
    if not sync_result:
        exception_logger.error(f'Failed to sync image groups: {sync_result.error}')
        await callback.message.edit_text('Ошибка при синхронизации групп.')
        await callback.answer()
        return

    active_groups = await uow.image_group_repository.get_groups()

    if not active_groups:
        await callback.message.edit_text('Нет активных групп.')
        await callback.answer()
        return

    await callback.message.edit_text(
        'Выберите группу для управления:',
        reply_markup=image_groups_keyboard(active_groups)
    )
    await state.set_state(ImageGroupsStates.selecting_group)
    await callback.answer()

async def show_group_details(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    group_name = callback.data.split('/')[1]

    group_result = await uow.image_group_repository.get_group_by_name(group_name)
    if not group_result:
        exception_logger.error(f'Failed to retrieve group "{group_name}": {group_result.error}')
        await callback.message.edit_text(f'Группа \'{group_name}\' не найдена.')
        await callback.answer()
        return

    group = group_result.value

    group_info = (
        f'Группа: {group.display_name}\n'
        f'Количество изображений: {group.count}\n'
        f'Статус: {'Активна' if group.status == GroupStatus.ACTIVE else 'Неактивна'}'
    )

    await callback.message.edit_text(group_info, reply_markup=group_action_keyboard(group))
    await state.update_data(group_name=group_name)
    await state.set_state(ImageGroupsStates.viewing_group_details)
    await callback.answer()

async def toggle_group_status(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    group_name = callback.data.split('/')[1]
    group = await uow.image_group_repository.get_group_by_name(group_name)

    if not group:
        await callback.message.edit_text(f'Группа \'{group_name}\' не найдена.')
        await callback.answer()
        return

    new_status = GroupStatus.INACTIVE if group.status == GroupStatus.ACTIVE else GroupStatus.ACTIVE
    await uow.image_group_repository.update_group_status(group_name, new_status)
    await show_group_details(callback, state, uow)

async def rename_group(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text('Введите новое название для группы:', reply_markup=back_to_image_groups_keyboard())
    await state.set_state(ImageGroupsStates.renaming_group)
    await callback.answer()

async def receive_new_group_name(message: types.Message, state: FSMContext, uow: MongoUnitOfWork):
    new_name = message.text
    data = await state.get_data()
    group_name = data['group_name']

    await uow.image_group_repository.update_group_name(group_name, new_name)
    await message.answer(f'Группа переименована в \'{new_name}\'.')

    await show_image_groups_menu(message, state, uow)

def register_handlers_image_groups(dp: Dispatcher, uow: MongoUnitOfWork):
    dp.callback_query.register(partial(show_image_groups_menu, uow=uow), lambda c: c.data == 'admin_panel:image_groups')
    dp.callback_query.register(partial(show_group_details, uow=uow), lambda c: c.data.startswith('group/'))
    dp.callback_query.register(partial(toggle_group_status, uow=uow), lambda c: c.data.startswith('toggle_status/'))
    dp.callback_query.register(rename_group, lambda c: c.data.startswith('rename/'))
    dp.message.register(partial(receive_new_group_name, uow=uow), ImageGroupsStates.renaming_group)
