from functools import partial
from aiogram import Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from src.commands.admin_panel.image_build_params.common_params.common_params_keyboard import (
    common_parameter_keyboard,
    common_back_keyboard,
    common_delete_keyboard
)
from src.commands.admin_panel.image_build_params.common_params.common_params_states import CommonParamsStates
from src.infrastructure.data.unit_of_work import MongoUnitOfWork
from src.models.image_build_params import CommonParam, ParamType

async def show_param_details(callback: CallbackQuery, uow: MongoUnitOfWork):
    param_type = callback.data.split('-')[1]
    async with uow:
        params_result = await uow.image_build_params_repository.get_common_params(ParamType(param_type))
        if params_result.is_success:
            params = params_result.value
            parameter_details = f'Параметры для "{param_type}":\n' + '\n'.join([f"{param.name}: {param.value}" for param in params])
            await callback.message.edit_text(parameter_details, reply_markup=common_parameter_keyboard(param_type))
        else:
            await callback.message.edit_text(f'Не удалось получить параметры для "{param_type}".', reply_markup=common_parameter_keyboard(param_type))
    await callback.answer()

async def add_param(callback: CallbackQuery, state: FSMContext):
    param_type = callback.data.split('-')[0]
    await state.update_data(param_type=param_type)
    await callback.message.edit_text(f'Введите название для нового значения параметра "{param_type}":', reply_markup=common_back_keyboard())
    await state.set_state(CommonParamsStates.waiting_for_name)
    await callback.answer()

async def receive_param_name(message: types.Message, state: FSMContext):
    await state.update_data(param_name=message.text)
    await message.answer(f'Введите значение для параметра "{message.text}":', reply_markup=common_back_keyboard())
    await state.set_state(CommonParamsStates.waiting_for_value)

async def receive_param_value(message: types.Message, state: FSMContext, uow: MongoUnitOfWork):
    data = await state.get_data()
    param_type = data['param_type']
    param_name = data['param_name']
    param_value = message.text

    param = CommonParam(name=param_name, value=float(param_value), type=ParamType(param_type))
    async with uow:
        param_result = await uow.image_build_params_repository.add_common_param(param)
        if param_result.is_success:
            await message.answer(f'Параметр "{param_name}" со значением "{param_value}" добавлен для типа "{param_type}".', reply_markup=common_back_keyboard())
        else:
            await message.answer(f'Не удалось добавить параметр "{param_name}" для типа "{param_type}".', reply_markup=common_back_keyboard())
        await uow.commit()
    await state.clear()

async def delete_param(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    param_type = callback.data.split('-')[0]
    async with uow:
        params_result = await uow.image_build_params_repository.get_common_params(ParamType(param_type))
        if params_result.is_success:
            params = params_result.value
            delete_options = f'Выберите параметр для удаления из "{param_type}":'
            await callback.message.edit_text(delete_options, reply_markup=common_delete_keyboard(params, param_type))
        else:
            await callback.message.edit_text(f'Не удалось получить параметры для "{param_type}".', reply_markup=common_back_keyboard())
    await state.set_state(CommonParamsStates.waiting_for_deletion_choice)
    await callback.answer()

async def confirm_delete_param(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    param_type = callback.data.split('-')[1]
    param_name = callback.data.split('-')[2]

    async with uow:
        delete_result = await uow.image_build_params_repository.delete_common_param(ParamType(param_type), param_name)
        if delete_result.is_success:
            await callback.message.edit_text(f'Параметр "{param_name}" удален из типа "{param_type}".', reply_markup=common_back_keyboard())
        else:
            await callback.message.edit_text(f'Не удалось удалить параметр "{param_name}" из типа "{param_type}".', reply_markup=common_back_keyboard())
        await uow.commit()
    await state.clear()
    await callback.answer()

def register_handlers_common_params(dp: Dispatcher, uow: MongoUnitOfWork):
    dp.callback_query.register(partial(show_param_details, uow=uow), lambda c: c.data.startswith('param-'))
    dp.callback_query.register(add_param, lambda c: c.data.endswith('-add'))
    dp.callback_query.register(partial(delete_param, uow=uow), lambda c: c.data.endswith('-delete'))
    dp.callback_query.register(partial(confirm_delete_param, uow=uow), lambda c: c.data.startswith('delete-'))
    dp.message.register(receive_param_name, CommonParamsStates.waiting_for_name)
    dp.message.register(partial(receive_param_value, uow=uow), CommonParamsStates.waiting_for_value)
