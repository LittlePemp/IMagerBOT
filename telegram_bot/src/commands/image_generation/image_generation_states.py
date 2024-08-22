
from aiogram.fsm.state import State, StatesGroup

class ImageGenerationStates(StatesGroup):
    selecting_image_size = State()
    selecting_noise_level = State()
    selecting_inset_size = State()
    selecting_image_group = State()
    waiting_for_image_upload = State()
