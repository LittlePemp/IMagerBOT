from src.commands.admin_panel.image_build_params.image_build_params_handler import \
    register_handlers_image_build_params
from src.commands.admin_panel.user_management import \
    register_handlers_user_management_total
from src.commands.image_generation.image_generation_handler import \
    register_handlers_image_generation
from src.commands.main_menu import MainMenuHandler
from src.commands.admin_panel_handler import AdminPanelHandler
from src.commands.user_management_handler import UserManagementHandler
from src.commands.image_build_params_handler import ImageBuildParamsHandler


def setup_handlers(dp, uow):
    ImageBuildParamsHandler.register_handlers(dp, uow)
    MainMenuHandler.register_handlers(dp)
    UserManagementHandler.register_handlers(dp)
    register_handlers_image_generation(dp, uow)
    AdminPanelHandler.register_handlers(dp)
