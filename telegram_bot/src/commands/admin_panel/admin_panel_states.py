from aiogram.fsm.state import State, StatesGroup


class AdminPanelStates(StatesGroup):
    managing_image_groups = State()
    managing_users = State()
