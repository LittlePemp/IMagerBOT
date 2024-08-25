from src.commands.admin_panel.admin_panel_handler import \
    register_handlers_admin_panel
from src.commands.admin_panel.image_build_params.image_build_params_handler import \
    register_handlers_image_build_params
from src.commands.admin_panel.user_management import \
    register_handlers_user_management_total
from src.commands.image_generation.image_generation_handler import \
    register_handlers_image_generation
from src.commands.main_menu import register_handlers_main_menu


def setup_handlers(dp, uow):
    register_handlers_main_menu(dp)
    register_handlers_admin_panel(dp)
    register_handlers_user_management_total(dp)
    register_handlers_image_build_params(dp, uow)
    register_handlers_image_generation(dp, uow)
