from aiogram.fsm.state import StatesGroup, State

class AdminPanelStates(StatesGroup):
    managing_image_groups = State()
    managing_users = State()
    settings = State()
