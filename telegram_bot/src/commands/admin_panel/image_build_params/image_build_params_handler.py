from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from src.utils.filters.request_filters.admin_filter import AdminFilter
from ..ui import AdminCallback, command, show
from .common_params.common_params_handler import register_handlers_common_params
from .image_groups.image_groups_handler import register_handlers_image_groups


async def render_parameters(event, state):
    await show(event, state, 'Выберите параметр для настройки:', [
        ('Размер результата', 'preset_list', 0),
        ('Уровень шума', 'preset_list', 1),
        ('Размер ячейки', 'preset_list', 2),
        ('Библиотеки изображений', 'groups', 0),
        ('Назад', 'home', 0),
    ])


async def show_image_build_params_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await render_parameters(message, state)


async def show_parameters_menu(callback, state: FSMContext):
    if await command(callback, state):
        await render_parameters(callback, state)


def register_handlers_image_build_params(dp: Dispatcher, uow):
    dp.message.register(show_image_build_params_menu, Command('admin_panel:image_params'), AdminFilter())
    dp.callback_query.register(show_parameters_menu, AdminCallback.filter(F.action == 'params'), AdminFilter())
    register_handlers_common_params(dp, uow)
    register_handlers_image_groups(dp, uow)
