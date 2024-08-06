from aiogram.fsm.state import State, StatesGroup

class CommonParamsStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_value = State()
    waiting_for_deletion_choice = State()
