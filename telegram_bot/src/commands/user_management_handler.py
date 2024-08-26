from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from src.infrastructure.data.repositories.user_repository import UserRepository
from src.models.user import User, UserStatus
from src.utils.loggers import bot_requests_logger, exception_logger

class UserManagementStates(StatesGroup):
    waiting_for_username_or_id = State()
    waiting_for_action = State()


class UserManagementHandler:

    @staticmethod
    async def start_user_management(callback_query: CallbackQuery, state: FSMContext):
        ''' Start user management. '''
        try:
            bot_requests_logger.info(f'User management started by user: {callback_query.from_user.id}')

            await callback_query.message.edit_text(
                'Введите username или ID пользователя, которого хотите управлять:'
            )
            await state.set_state(UserManagementStates.waiting_for_username_or_id)
            await callback_query.answer()

        except Exception as e:
            exception_logger.error(f'Error in start_user_management: {e}')
            await callback_query.message.answer('Ошибка при запуске управления пользователями.')
            await callback_query.answer()

    @staticmethod
    async def receive_username_or_id(message: Message, state: FSMContext):
        ''' Handle user by username or ID. '''
        try:
            user_repository: UserRepository = message.bot.user_repository
            user_input = message.text

            user = await UserManagementHandler.fetch_user(user_input, user_repository)

            if not user:
                await message.answer('Пользователь не найден.')
                await state.clear()
                return

            user_info = UserManagementHandler.format_user_info(user)
            await state.update_data(user_id=user.telegram_id, isbanned=user.isbanned,
                                    is_admin=(user.status == UserStatus.ADMIN))
            await message.answer(
                user_info,
                reply_markup=UserManagementHandler.user_action_keyboard(user.isbanned, user.status == UserStatus.ADMIN))
            await state.set_state(UserManagementStates.waiting_for_action)
        except Exception as e:
            exception_logger.error(f'Error in receive_username_or_id: {e}')
            await message.answer('Ошибка при обработке информации о пользователе.')
            await state.clear()

    @staticmethod
    async def handle_user_action(callback_query: CallbackQuery, state: FSMContext):
        ''' Handle user action by button callback '''
        data = await state.get_data()
        action = callback_query.data.split(':')[1]

        if action == 'block_user' or action == 'unblock_user':
            await UserManagementHandler.block_unblock_user(callback_query, state)
        elif action == 'make_admin' or action == 'remove_admin':
            await UserManagementHandler.make_remove_admin(callback_query, state)

    @staticmethod
    async def block_unblock_user(callback_query: CallbackQuery, state: FSMContext):
        ''' Handle block/unblock '''
        try:
            data = await state.get_data()
            user_repository: UserRepository = callback_query.bot.user_repository
            user_id = data['user_id']
            isbanned = data['isbanned']
            new_status = not isbanned

            await user_repository.update_user(user_id, {'isbanned': new_status})
            user = await user_repository.get_user_by_id(user_id)
            user_info = UserManagementHandler.format_user_info(user)
            await callback_query.message.edit_text(user_info,
                                                   reply_markup=UserManagementHandler.user_action_keyboard(
                                                       user.isbanned, user.status == UserStatus.ADMIN))
            await state.update_data(isbanned=new_status)
            await callback_query.answer()
        except Exception as e:
            exception_logger.error(f'Error in block_unblock_user: {e}')
            await callback_query.message.answer('Ошибка при изменении статуса блокировки пользователя.')
            await callback_query.answer()

    @staticmethod
    async def make_remove_admin(callback_query: CallbackQuery, state: FSMContext):
        ''' Handle admin/unadmin user. '''
        try:
            data = await state.get_data()
            user_repository: UserRepository = callback_query.bot.user_repository
            user_id = data['user_id']
            is_admin = data['is_admin']
            new_status = UserStatus.ADMIN if not is_admin else UserStatus.USER

            await user_repository.update_user(user_id, {'status': new_status})
            user = await user_repository.get_user_by_id(user_id)
            user_info = UserManagementHandler.format_user_info(user)
            await callback_query.message.edit_text(user_info,
                                                   reply_markup=UserManagementHandler.user_action_keyboard(
                                                       user.isbanned, user.status == UserStatus.ADMIN))
            await state.update_data(is_admin=(new_status == UserStatus.ADMIN))
            await callback_query.answer()
        except Exception as e:
            exception_logger.error(f'Error in make_remove_admin: {e}')
            await callback_query.message.answer('Ошибка при изменении статуса администратора.')
            await callback_query.answer()

    @staticmethod
    def user_action_keyboard(isbanned: bool, is_admin: bool) -> InlineKeyboardMarkup:
        ''' User action keyboard '''
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text='Разблокировать' if isbanned else 'Заблокировать',
                                     callback_data=f'user_action:{'unblock_user' if isbanned else 'block_user'}'),
                InlineKeyboardButton(text='Снять администратора' if is_admin else 'Назначить администратора',
                                     callback_data=f'user_action:{'remove_admin' if is_admin else 'make_admin'}')
            ]
        ])
        return keyboard

    @staticmethod
    def format_user_info(user: User) -> str:
        ''' User info format '''
        return (
            f'Имя пользователя: {user.telegram_username}\n'
            f'ID пользователя: {user.telegram_id}\n'
            f'Зарегистрирован: {user.registered_datetime_utc}\n'
            f'Последняя активность: {user.last_activity_datetime_utc}\n'
            f'Заблокирован: {user.isbanned}\n'
            f'Статус: {user.status.value}'
        )

    @staticmethod
    async def fetch_user(user_input: str, user_repository: UserRepository):
        ''' Get user by ID or username '''
        try:
            if user_input.isdigit():
                return await user_repository.get_user_by_id(int(user_input))
            else:
                user = await user_repository.get_user_by_username(user_input)
                if user:
                    await user_repository.update_user(user.telegram_id, {'telegram_username': user_input})
                return user
        except Exception as e:
            exception_logger.error(f'Error in fetch_user: {e}')
            return None

    @staticmethod
    def register_handlers(dp: Dispatcher):
        dp.callback_query.register(UserManagementHandler.start_user_management,
                                   F.data == 'admin_panel:users')
        dp.message.register(UserManagementHandler.receive_username_or_id,
                            UserManagementStates.waiting_for_username_or_id)
        dp.callback_query.register(UserManagementHandler.handle_user_action,
                                   F.data.startswith('user_action:'),
                                   UserManagementStates.waiting_for_action)
