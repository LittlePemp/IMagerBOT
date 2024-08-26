from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardMarkup
from src.utils.filters.request_filters.admin_filter import AdminFilter
from src.utils.loggers import bot_requests_logger, exception_logger


class AdminPanelStates(StatesGroup):
    managing_image_groups = State()
    managing_users = State()


class AdminPanelKeyboard:
    @staticmethod
    def get_keyboard() -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text='Пользователи', callback_data='admin_panel:users')
        builder.button(text='Параметры сборки изображения', callback_data='admin_panel:image_params')
        builder.adjust(1)
        return builder.as_markup()


class AdminPanelHandler:
    @staticmethod
    async def admin_panel(message: types.Message):
        try:
            bot_requests_logger.info(f'Admin panel command triggered by user: {message.from_user.id}')
            await message.answer('Панель администратора', reply_markup=AdminPanelKeyboard.get_keyboard())
        except Exception as e:
            exception_logger.error(f'Error in admin_panel command: {e}')
            await message.answer('Ошибка при открытии панели администратора.')

    @staticmethod
    async def admin_panel_callback(callback_query: types.CallbackQuery, state: FSMContext):
        try:
            bot_requests_logger.info(f'Admin panel callback triggered by user: {callback_query.from_user.id}')

            new_text = 'Панель администратора'
            new_markup = AdminPanelKeyboard.get_keyboard()

            await callback_query.message.answer(new_text, reply_markup=new_markup)

            await callback_query.answer()

            match callback_query.data:
                case 'admin_panel:users':
                    await state.set_state(AdminPanelStates.managing_users)
                case 'admin_panel:image_params':
                    await state.set_state(AdminPanelStates.managing_image_groups)

        except Exception as e:
            exception_logger.error(f'Error in admin_panel_callback: {e}')
            await callback_query.message.answer('Ошибка при обработке команды панели администратора.')
            await callback_query.answer()

    @staticmethod
    def register_handlers(dp: Dispatcher):
        dp.message.register(AdminPanelHandler.admin_panel, Command('admin'), AdminFilter())
        dp.callback_query.register(AdminPanelHandler.admin_panel_callback,
                                   F.data.startswith('admin_panel'), AdminFilter())
