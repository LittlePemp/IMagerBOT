"""Registration only; conversation and photo admission have separate modules."""
from aiogram import F
from aiogram.filters import StateFilter
from .image_generation_states import ImageGenerationStates as S
from . import wizard
from .upload import receive_image, generation_job_id as _generation_job_id


def register_handlers_image_generation(dp, uow):
    dp.callback_query.register(wizard.start, F.data == 'generate_image')
    dp.callback_query.register(wizard.choose_mode, F.data.in_({'mode:manual', 'mode:presets'}), S.choosing_generation_mode)
    presets = StateFilter(S.selecting_image_size, S.selecting_noise_level, S.selecting_inset_size)
    dp.callback_query.register(wizard.pick_preset, F.data.startswith('pick:'), presets)
    dp.callback_query.register(wizard.preset_page, F.data.startswith('preset_page:'), presets)
    dp.callback_query.register(wizard.choose_group, F.data.startswith('group:'), S.selecting_image_group)
    dp.callback_query.register(wizard.group_page, F.data.startswith('group_page:'), S.selecting_image_group)
    dp.message.register(wizard.manual_value, F.text, StateFilter(
        S.waiting_manual_image_size, S.waiting_manual_noise_level, S.waiting_manual_inset_size,
    ))
    dp.callback_query.register(wizard.back, F.data == 'back', StateFilter(
        S.selecting_image_size, S.selecting_noise_level, S.selecting_inset_size,
        S.waiting_manual_image_size, S.waiting_manual_noise_level, S.waiting_manual_inset_size,
        S.selecting_image_group, S.waiting_for_image_upload,
    ))
    dp.message.register(receive_image, F.photo, S.waiting_for_image_upload)
