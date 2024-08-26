from aiogram import Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup)
from src.infrastructure.data.unit_of_work import MongoUnitOfWork
from src.utils.loggers import bot_requests_logger, exception_logger

from .common_params_handler import CommonParamsHandler
from .image_groups_handler import ImageGroupsHandler


class ImageBuildParamsStates(StatesGroup):
    selecting_param = State()


class ImageBuildParamsHandler:
    @staticmethod
    async def show_image_build_params_menu(message: types.Message, state: FSMContext):
        try:
            await state.clear()
            await message.answer('Выберите параметр для настройки:',
                                 reply_markup=ImageBuildParamsHandler.get_keyboard())
        except Exception as e:
            exception_logger.error(f'Error showing image build params menu: {e}')
            await message.answer('Произошла ошибка при отображении меню параметров сборки изображений.')

    @staticmethod
    async def show_parameters_menu(callback: CallbackQuery, state: FSMContext):
        try:
            await state.clear()
            await callback.message.edit_text('Выберите параметр:', reply_markup=ImageBuildParamsHandler.get_keyboard())
            await callback.answer()
        except Exception as e:
            exception_logger.error(f'Error showing parameters menu: {e}')
            await callback.message.answer('Произошла ошибка при отображении меню параметров.')
            await callback.answer()

    @staticmethod
    def get_keyboard():
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text='Размер изображения', callback_data='param:image_size'),
                InlineKeyboardButton(text='Уровень шума', callback_data='param:noise_level')
            ],
            [
                InlineKeyboardButton(text='Размер вставки', callback_data='param:inset_size'),
                InlineKeyboardButton(text='Группы изображений', callback_data='admin_panel:image_groups')
            ],
            [
                InlineKeyboardButton(text='Назад', callback_data='admin_panel')
            ]
        ])
        return keyboard

    @staticmethod
    def register_handlers(dp: Dispatcher, uow: MongoUnitOfWork):
        dp.message.register(ImageBuildParamsHandler.show_image_build_params_menu,
                            Command('admin_panel:image_params'))
        dp.callback_query.register(ImageBuildParamsHandler.show_parameters_menu,
                                   lambda c: c.data == 'admin_panel:image_params')

        ImageGroupsHandler.register_handlers(dp, uow)
        CommonParamsHandler.register_handlers(dp, uow)
