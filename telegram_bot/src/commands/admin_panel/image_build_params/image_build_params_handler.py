from aiogram import Dispatcher, types
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import CallbackQuery
from src.commands.admin_panel.image_build_params.image_build_params_keyboard import (
    image_build_params_keyboard
)
from src.commands.admin_panel.image_build_params.common_params.common_params_handler import register_handlers_common_params
from src.infrastructure.data.unit_of_work import MongoUnitOfWork

async def show_image_build_params_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer('Выберите параметр для настройки:', reply_markup=image_build_params_keyboard())

async def show_parameters_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text('Выберите параметр:', reply_markup=image_build_params_keyboard())
    await callback.answer()

def register_handlers_image_build_params(dp: Dispatcher, uow: MongoUnitOfWork):
    dp.message.register(show_image_build_params_menu, Command('admin_panel:image_params'))
    dp.callback_query.register(show_parameters_menu, lambda c: c.data == 'admin_panel:image_params')
    
    register_handlers_common_params(dp, uow)
