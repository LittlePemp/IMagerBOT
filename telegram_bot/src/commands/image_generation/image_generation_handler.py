from functools import partial

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from settings import settings
from src.commands.main_menu import main_menu
from src.infrastructure.data.unit_of_work import MongoUnitOfWork
from src.infrastructure.services.imager_service import imager_service
from src.infrastructure.services.imager_service.schemas import \
    GenerateImageRequest
from src.models.image_build_params import ParamType
from src.models.user import User
from src.utils.loggers import bot_requests_logger, exception_logger

from .image_generation_keyboards import (image_group_keyboard,
                                         image_size_keyboard,
                                         inset_size_keyboard,
                                         noise_level_keyboard)
from .image_generation_states import ImageGenerationStates


async def start_image_generation(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    await state.clear()
    sizes_result = await uow.image_build_params_repository.get_common_params(ParamType.IMAGE_SIZE)
    if not sizes_result:
        await callback.message.edit_text('Ошибка при получении размеров изображений.')
        await callback.answer()
        return

    sizes = sizes_result.value
    await callback.message.edit_text('Выберите размер изображения:', reply_markup=image_size_keyboard(sizes).as_markup())
    await state.set_state(ImageGenerationStates.selecting_image_size)
    await callback.answer()


async def select_image_size(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    size_name = callback.data.split('_')[2]
    await state.update_data(image_size=size_name)

    noises_result = await uow.image_build_params_repository.get_common_params(ParamType.NOISE_LEVEL)
    if noises_result.is_success:
        noises = noises_result.value
        await callback.message.edit_text('Выберите уровень шума:', reply_markup=noise_level_keyboard(noises).as_markup())
        await state.set_state(ImageGenerationStates.selecting_noise_level)
    else:
        await callback.message.edit_text('Ошибка получения уровней шума.')
    await callback.answer()


async def select_noise_level(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    noise_name = callback.data.split('_')[2]
    await state.update_data(noise_level=noise_name)

    insets_result = await uow.image_build_params_repository.get_common_params(ParamType.INSET_SIZE)
    if insets_result.is_success:
        insets = insets_result.value
        await callback.message.edit_text('Выберите размер вставки:', reply_markup=inset_size_keyboard(insets).as_markup())
        await state.set_state(ImageGenerationStates.selecting_inset_size)
    else:
        await callback.message.edit_text('Ошибка получения размеров вставок.')
    await callback.answer()


async def select_inset_size(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    inset_name = callback.data.split('_')[2]
    await state.update_data(inset_size=inset_name)

    groups_result = await uow.image_group_repository.get_groups()
    if groups_result.is_success:
        groups = groups_result.value
        await callback.message.edit_text('Выберите группу изображений:',
                                         reply_markup=image_group_keyboard(groups).as_markup())
        await state.set_state(ImageGenerationStates.selecting_image_group)
    else:
        await callback.message.edit_text('Ошибка получения групп изображений.')
    await callback.answer()


async def select_image_group(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    group_name = callback.data.split('/')[1]
    await state.update_data(image_group=group_name)
    await callback.message.edit_text('Загрузите изображение для обработки:')
    await state.set_state(ImageGenerationStates.waiting_for_image_upload)
    await callback.answer()


async def receive_image(message: Message, state: FSMContext, uow: MongoUnitOfWork, user: User):
    # Check image
    if not message.photo:
        await message.answer('Пожалуйста, загрузите изображение.')
        return

    # try to load image
    try:
        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)
        file_path = f'{settings.uploaded_images_path}/{file.file_unique_id}.jpg'
        await message.bot.download_file(file.file_path, file_path)
        await state.update_data(image_path=file_path)
    except Exception as e:
        exception_logger.error(f'Ошибка при загрузке изображения: {e}')
        await message.answer('Внутренняя ошибка: не удалось загрузить изображение. Пожалуйста, попробуйте еще раз.')
        return

    # Image generation
    try:
        data = await state.get_data()
        generate_image_request = GenerateImageRequest(
            image_path=data['image_path'],
            group_name=data['image_group'],
            insertion_format='crop',
            alpha_channel=30,
            noise_level=int(float(data['noise_level'])),
            cell_size=int(float(data['inset_size'])),
            result_size=int(float(data['image_size'])),
        )

        # Handle image generation
        imager_response_result = await imager_service.generate_image(generate_image_request)
        if not imager_response_result:
            exception_logger.error(f'Ошибка при генерации изображения: {imager_response_result.error}')
            await message.answer(
                'Внутренняя ошибка: не удалось сгенерировать изображение. Пожалуйста, попробуйте еще раз.')
            return

        # Image generation path
        imager_response = imager_response_result.value
        generated_image_path = f'{settings.generated_images_path}/{imager_response.path}'

        # Send image
        input_file = FSInputFile(generated_image_path)
        await message.answer_document(document=input_file, caption='Ваше изображение готово!')
    except Exception as e:
        exception_logger.error(f'Ошибка при генерации изображения: {e}')
        await message.answer('Внутренняя ошибка: не удалось сгенерировать изображение. Пожалуйста, попробуйте еще раз.')
        return

    # FSM clear
    await state.clear()
    bot_requests_logger.info('FSM state cleared after image generation.')

    await main_menu(message, user)

async def back_to_previous(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    current_state = await state.get_state()
    bot_requests_logger.info(f'Current FSM state: {current_state}')

    if current_state == ImageGenerationStates.selecting_noise_level:
        sizes_result = await uow.image_build_params_repository.get_common_params(ParamType.IMAGE_SIZE)
        sizes = sizes_result.value
        await callback.message.edit_text('Выберите размер изображения:',
                                         reply_markup=image_size_keyboard(sizes).as_markup())
        await state.set_state(ImageGenerationStates.selecting_image_size)

    elif current_state == ImageGenerationStates.selecting_inset_size:
        noises_result = await uow.image_build_params_repository.get_common_params(ParamType.NOISE_LEVEL)
        noises = noises_result.value
        await callback.message.edit_text('Выберите уровень шума:', reply_markup=noise_level_keyboard(noises).as_markup())
        await state.set_state(ImageGenerationStates.selecting_noise_level)

    elif current_state == ImageGenerationStates.selecting_image_group:
        insets_result = await uow.image_build_params_repository.get_common_params(ParamType.INSET_SIZE)
        insets = insets_result.value
        await callback.message.edit_text('Выберите размер вставки:', reply_markup=inset_size_keyboard(insets).as_markup())
        await state.set_state(ImageGenerationStates.selecting_inset_size)

    elif current_state == ImageGenerationStates.waiting_for_image_upload:
        groups_result = await uow.image_group_repository.get_groups()
        groups = groups_result.value
        await callback.message.edit_text('Выберите группу изображений:',
                                         reply_markup=image_group_keyboard(groups).as_markup())
        await state.set_state(ImageGenerationStates.selecting_image_group)

    await callback.answer()


def register_handlers_image_generation(dp: Dispatcher, uow: MongoUnitOfWork):
    dp.callback_query.register(partial(start_image_generation, uow=uow), F.data == 'generate_image')
    dp.callback_query.register(partial(select_image_size, uow=uow), lambda c: c.data.startswith('image_size_'))
    dp.callback_query.register(partial(select_noise_level, uow=uow), lambda c: c.data.startswith('noise_level_'))
    dp.callback_query.register(partial(select_inset_size, uow=uow), lambda c: c.data.startswith('inset_size_'))
    dp.callback_query.register(partial(select_image_group, uow=uow), lambda c: c.data.startswith('image_group/'))
    dp.message.register(partial(receive_image, uow=uow), ImageGenerationStates.waiting_for_image_upload)
    dp.callback_query.register(partial(back_to_previous, uow=uow), lambda c: c.data == 'back')
