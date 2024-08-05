from aiogram import Dispatcher
from src.commands.admin_panel.user_management.user_actions.block_unblock_handler import register_handlers_block_unblock
from src.commands.admin_panel.user_management.user_actions.make_remove_admin_handler import register_handlers_make_remove_admin
from src.commands.admin_panel.user_management.user_management_handler import register_handlers_user_management

def register_handlers_user_management_total(dp: Dispatcher):
    register_handlers_user_management(dp)
    register_handlers_block_unblock(dp)
    register_handlers_make_remove_admin(dp)
