from aiogram.fsm.state import State, StatesGroup

class UserManagementStates(StatesGroup):
    waiting_for_username_or_id = State()
    waiting_for_action = State()
