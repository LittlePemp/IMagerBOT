from functools import partial
from aiogram import Dispatcher, types
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from src.infrastructure.data.unit_of_work import MongoUnitOfWork
from src.models.image_build_params import CommonParam, ParamType
from src.utils.loggers import exception_logger
from aiogram.fsm.state import State, StatesGroup

class CommonParamsStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_value = State()
    waiting_for_deletion_choice = State()


class CommonParamsHandler:

    @staticmethod
    async def show_param_details(callback: CallbackQuery, uow: MongoUnitOfWork):
        param_type = callback.data.split(':')[1]
        try:
            async with uow:
                params_result = await uow.image_build_params_repository.get_common_params(ParamType(param_type))
                if params_result.is_success:
                    params = params_result.value
                    parameter_details = (
                        f'Параметры для "{param_type}":\n'
                        + '\n'.join([f"{param.name}: {param.value}" for param in params]))
                    await callback.message.edit_text(parameter_details,
                                                     reply_markup=CommonParamsHandler.get_parameter_keyboard(
                                                         param_type))
                else:
                    await callback.message.edit_text(f'Не удалось получить параметры для "{param_type}".',
                                                     reply_markup=CommonParamsHandler.get_parameter_keyboard(
                                                         param_type))
        except Exception as e:
            exception_logger.error(f'Error showing param details for {param_type}: {e}')
            await callback.message.answer('Произошла ошибка при отображении параметров.')
        await callback.answer()

    @staticmethod
    async def add_param(callback: CallbackQuery, state: FSMContext):
        param_type = callback.data.split(':')[0]
        try:
            await state.update_data(param_type=param_type)
            await callback.message.edit_text(f'Введите название для нового значения параметра "{param_type}":',
                                             reply_markup=CommonParamsHandler.get_back_keyboard())
            await state.set_state(CommonParamsStates.waiting_for_name)
        except Exception as e:
            exception_logger.error(f'Error initiating add_param for {param_type}: {e}')
            await callback.message.answer('Произошла ошибка при добавлении параметра.')
        await callback.answer()

    @staticmethod
    async def receive_param_name(message: types.Message, state: FSMContext):
        try:
            await state.update_data(param_name=message.text)
            await message.answer(f'Введите значение для параметра "{message.text}":',
                                 reply_markup=CommonParamsHandler.get_back_keyboard())
            await state.set_state(CommonParamsStates.waiting_for_value)
        except Exception as e:
            exception_logger.error(f'Error receiving param name: {e}')
            await message.answer('Произошла ошибка при получении имени параметра.')

    @staticmethod
    async def receive_param_value(message: types.Message, state: FSMContext, uow: MongoUnitOfWork):
        try:
            data = await state.get_data()
            param_type = data['param_type']
            param_name = data['param_name']
            param_value = message.text

            param = CommonParam(name=param_name, value=float(param_value), type=ParamType(param_type))
            async with uow:
                param_result = await uow.image_build_params_repository.add_common_param(param)
                if param_result.is_success:
                    await message.answer(
                        f'Параметр "{param_name}" со значением "{param_value}" добавлен для типа "{param_type}".',
                        reply_markup=CommonParamsHandler.get_back_keyboard())
                else:
                    await message.answer(
                        f'Не удалось добавить параметр "{param_name}" для типа "{param_type}".',
                        reply_markup=CommonParamsHandler.get_back_keyboard())
                await uow.commit()
            await state.clear()
        except Exception as e:
            exception_logger.error(f'Error receiving param value: {e}')
            await message.answer('Произошла ошибка при добавлении значения параметра.')

    @staticmethod
    async def delete_param(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
        param_type = callback.data.split(':')[0]
        try:
            async with uow:
                params_result = await uow.image_build_params_repository.get_common_params(ParamType(param_type))
                if params_result.is_success:
                    params = params_result.value
                    delete_options = f'Выберите параметр для удаления из "{param_type}":'
                    await callback.message.edit_text(delete_options,
                                                     reply_markup=CommonParamsHandler.get_delete_keyboard(params,
                                                                                                          param_type))
                else:
                    await callback.message.edit_text(f'Не удалось получить параметры для "{param_type}".',
                                                     reply_markup=CommonParamsHandler.get_back_keyboard())
            await state.set_state(CommonParamsStates.waiting_for_deletion_choice)
        except Exception as e:
            exception_logger.error(f'Error deleting param for {param_type}: {e}')
            await callback.message.answer('Произошла ошибка при удалении параметра.')
        await callback.answer()

    @staticmethod
    async def confirm_delete_param(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
        try:
            param_type = callback.data.split(':')[1]
            param_name = callback.data.split(':')[2]

            async with uow:
                delete_result = await uow.image_build_params_repository.delete_common_param(ParamType(param_type),
                                                                                            param_name)
                if delete_result.is_success:
                    await callback.message.edit_text(f'Параметр "{param_name}" удален из типа "{param_type}".',
                                                     reply_markup=CommonParamsHandler.get_back_keyboard())
                else:
                    await callback.message.edit_text(
                        f'Не удалось удалить параметр "{param_name}" из типа "{param_type}".',
                        reply_markup=CommonParamsHandler.get_back_keyboard())
                await uow.commit()
            await state.clear()
        except Exception as e:
            exception_logger.error(f'Error confirming param deletion: {e}')
            await callback.message.answer('Произошла ошибка при подтверждении удаления параметра.')
        await callback.answer()

    @staticmethod
    def get_parameter_keyboard(param_type: str):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='Добавить', callback_data=f'{param_type}:add')],
            [InlineKeyboardButton(text='Удалить', callback_data=f'{param_type}:delete')],
            [InlineKeyboardButton(text='Назад', callback_data='admin_panel:image_params')]
        ])
        return keyboard

    @staticmethod
    def get_back_keyboard():
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='Назад', callback_data='admin_panel:image_params')]
        ])
        return keyboard

    @staticmethod
    def get_delete_keyboard(params, param_type):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f'{param.name}: {param.value}',
                                  callback_data=f'delete:{param_type}:{param.name}')] for param in params
        ])
        keyboard.inline_keyboard.append([InlineKeyboardButton(text='Назад', callback_data='admin_panel:image_params')])
        return keyboard

    @staticmethod
    def register_handlers(dp: Dispatcher, uow: MongoUnitOfWork):
        dp.callback_query.register(partial(CommonParamsHandler.show_param_details, uow=uow),
                                   lambda c: c.data.startswith('param:'))
        dp.callback_query.register(CommonParamsHandler.add_param,
                                   lambda c: c.data.endswith(':add'))
        dp.callback_query.register(partial(CommonParamsHandler.delete_param, uow=uow),
                                   lambda c: c.data.endswith(':delete'))
        dp.callback_query.register(partial(CommonParamsHandler.confirm_delete_param, uow=uow),
                                   lambda c: c.data.startswith('delete:'))
        dp.message.register(CommonParamsHandler.receive_param_name,
                            CommonParamsStates.waiting_for_name)
        dp.message.register(partial(CommonParamsHandler.receive_param_value, uow=uow),
                            CommonParamsStates.waiting_for_value)
