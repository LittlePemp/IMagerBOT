from aiogram.fsm.state import State, StatesGroup

class ImageGroupsStates(StatesGroup):
    selecting_group = State()
    viewing_group_details = State()
    renaming_group = State()
