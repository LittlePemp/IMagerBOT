import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from datetime import datetime
from functools import partial

from aiogram import Dispatcher, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

try:
    from aiogram.types import ReactionTypeEmoji
except ImportError:  # pragma: no cover
    ReactionTypeEmoji = None

from settings import settings
from src.commands.common_keyboards import home_keyboard
from src.commands.main_menu import MAIN_MENU_TEXT, build_main_menu_keyboard
from src.infrastructure.data.unit_of_work import MongoUnitOfWork
from src.infrastructure.services.imager_service import imager_service
from src.infrastructure.services.imager_service.schemas import GenerateImageRequest
from src.models.generation_history import GenerationHistoryRecord, generation_history_record_id
from src.models.image_build_params import ParamType
from src.models.user import User
from src.utils.loggers import bot_requests_logger, exception_logger

from .image_generation_keyboards import (
    generation_mode_keyboard,
    image_group_keyboard,
    image_size_keyboard,
    inset_size_keyboard,
    noise_level_keyboard,
    upload_image_keyboard,
)
from .image_generation_states import ImageGenerationStates


GENERATION_MODE_PRESETS = "presets"
GENERATION_MODE_MANUAL = "manual"
PROCESSING_REACTION = "👀"


def _format_number(value: float | int) -> str:
    numeric_value = float(value)
    if numeric_value.is_integer():
        return str(int(numeric_value))
    return str(numeric_value)


def _manual_defaults_from_preferences(preferences: dict | None) -> dict:
    return {
        "image_size": int((preferences or {}).get("image_size", settings.generation_default_result_size)),
        "noise_level": int((preferences or {}).get("noise_level", settings.generation_default_noise_level)),
        "inset_size": int((preferences or {}).get("inset_size", settings.generation_default_cell_size)),
    }


def _build_output_limit_error(result_size: int, cell_size: int) -> str:
    output_side = result_size * cell_size
    estimated_pixels = output_side ** 2
    estimated_bytes = int(
        estimated_pixels * settings.generation_estimated_jpeg_bytes_per_pixel
    )
    return (
        "Итоговое изображение, скорее всего, будет слишком большим для Telegram.\n"
        f"Оценка стороны: {result_size} x {cell_size} = {output_side} пикселей.\n"
        f"Оценка площади: {output_side}^2 = {estimated_pixels} пикселей.\n"
        f"Оценка веса JPEG: {estimated_bytes} байт.\n"
        f"Лимит Telegram: {settings.telegram_max_document_size_bytes} байт.\n"
        "Попробуйте уменьшить размер изображения или размер ячейки."
    )


def _validate_output_side(result_size: int, cell_size: int) -> str | None:
    estimated_pixels = (result_size * cell_size) ** 2
    estimated_bytes = int(
        estimated_pixels * settings.generation_estimated_jpeg_bytes_per_pixel
    )
    if estimated_bytes > settings.telegram_max_document_size_bytes:
        return _build_output_limit_error(result_size, cell_size)
    return None


async def _set_processing_reaction(message: Message):
    if ReactionTypeEmoji is None:
        return
    try:
        await message.bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=PROCESSING_REACTION)],
            is_big=False,
        )
    except Exception:
        bot_requests_logger.warning("Failed to set processing reaction on message")


async def _show_generation_mode(message_obj, state: FSMContext):
    await state.clear()
    await message_obj.edit_text(
        "Выберите, как задать параметры сборки:",
        reply_markup=generation_mode_keyboard().as_markup(),
    )
    await state.set_state(ImageGenerationStates.choosing_generation_mode)


async def _show_image_sizes(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    sizes_result = await uow.image_build_params_repository.get_common_params(ParamType.IMAGE_SIZE)
    if not sizes_result.is_success or not sizes_result.value:
        await callback.message.edit_text(
            "Не удалось получить доступные размеры изображения.",
            reply_markup=home_keyboard(),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "Выберите размер итогового изображения:",
        reply_markup=image_size_keyboard(sizes_result.value).as_markup(),
    )
    await state.set_state(ImageGenerationStates.selecting_image_size)
    await callback.answer()


async def _show_noise_levels(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    noises_result = await uow.image_build_params_repository.get_common_params(ParamType.NOISE_LEVEL)
    if not noises_result.is_success or not noises_result.value:
        await callback.message.edit_text(
            "Не удалось получить доступные уровни шума.",
            reply_markup=home_keyboard(),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "Выберите уровень шума:",
        reply_markup=noise_level_keyboard(noises_result.value).as_markup(),
    )
    await state.set_state(ImageGenerationStates.selecting_noise_level)
    await callback.answer()


async def _show_inset_sizes(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    insets_result = await uow.image_build_params_repository.get_common_params(ParamType.INSET_SIZE)
    if not insets_result.is_success or not insets_result.value:
        await callback.message.edit_text(
            "Не удалось получить доступные размеры вставки.",
            reply_markup=home_keyboard(),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "Выберите размер ячейки:",
        reply_markup=inset_size_keyboard(insets_result.value).as_markup(),
    )
    await state.set_state(ImageGenerationStates.selecting_inset_size)
    await callback.answer()


async def _show_groups(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    groups_result = await uow.image_group_repository.get_groups()
    if not groups_result.is_success or not groups_result.value:
        await callback.message.edit_text(
            "Не удалось получить группы изображений.",
            reply_markup=home_keyboard(),
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "Выберите группу изображений:",
        reply_markup=image_group_keyboard(groups_result.value).as_markup(),
    )
    await state.set_state(ImageGenerationStates.selecting_image_group)
    await callback.answer()


async def _ask_manual_image_size(message_obj, state: FSMContext):
    data = await state.get_data()
    defaults = data.get("manual_defaults", _manual_defaults_from_preferences(None))
    await message_obj.answer(
        (
            "Введите размер итогового изображения.\n"
            f"Допустимый диапазон: {settings.generation_result_size_min}-{settings.generation_result_size_max}.\n"
            f"Рекомендуемое значение: {defaults['image_size']}."
        ),
        reply_markup=upload_image_keyboard().as_markup(),
    )
    await state.set_state(ImageGenerationStates.waiting_manual_image_size)


async def _ask_manual_noise_level(message_obj, state: FSMContext):
    data = await state.get_data()
    defaults = data.get("manual_defaults", _manual_defaults_from_preferences(None))
    await message_obj.answer(
        (
            "Введите уровень шума.\n"
            f"Допустимый диапазон: {settings.generation_noise_level_min}-{settings.generation_noise_level_max}.\n"
            f"Рекомендуемое значение: {defaults['noise_level']}."
        ),
        reply_markup=upload_image_keyboard().as_markup(),
    )
    await state.set_state(ImageGenerationStates.waiting_manual_noise_level)


async def _ask_manual_inset_size(message_obj, state: FSMContext):
    data = await state.get_data()
    defaults = data.get("manual_defaults", _manual_defaults_from_preferences(None))
    await message_obj.answer(
        (
            "Введите размер ячейки.\n"
            f"Допустимый диапазон: {settings.generation_cell_size_min}-{settings.generation_cell_size_max}.\n"
            f"Рекомендуемое значение: {defaults['inset_size']}."
        ),
        reply_markup=upload_image_keyboard().as_markup(),
    )
    await state.set_state(ImageGenerationStates.waiting_manual_inset_size)


async def _edit_manual_image_size_prompt(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    defaults = data.get("manual_defaults", _manual_defaults_from_preferences(None))
    await callback.message.edit_text(
        (
            "Введите размер итогового изображения.\n"
            f"Допустимый диапазон: {settings.generation_result_size_min}-{settings.generation_result_size_max}.\n"
            f"Рекомендуемое значение: {defaults['image_size']}."
        ),
        reply_markup=upload_image_keyboard().as_markup(),
    )
    await state.set_state(ImageGenerationStates.waiting_manual_image_size)


async def _edit_manual_noise_level_prompt(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    defaults = data.get("manual_defaults", _manual_defaults_from_preferences(None))
    await callback.message.edit_text(
        (
            "Введите уровень шума.\n"
            f"Допустимый диапазон: {settings.generation_noise_level_min}-{settings.generation_noise_level_max}.\n"
            f"Рекомендуемое значение: {defaults['noise_level']}."
        ),
        reply_markup=upload_image_keyboard().as_markup(),
    )
    await state.set_state(ImageGenerationStates.waiting_manual_noise_level)


async def _edit_manual_inset_size_prompt(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    defaults = data.get("manual_defaults", _manual_defaults_from_preferences(None))
    await callback.message.edit_text(
        (
            "Введите размер ячейки.\n"
            f"Допустимый диапазон: {settings.generation_cell_size_min}-{settings.generation_cell_size_max}.\n"
            f"Рекомендуемое значение: {defaults['inset_size']}."
        ),
        reply_markup=upload_image_keyboard().as_markup(),
    )
    await state.set_state(ImageGenerationStates.waiting_manual_inset_size)


def _parse_manual_int(raw_value: str | None, field_name: str, min_value: int, max_value: int) -> tuple[bool, int | str]:
    value = (raw_value or "").strip()
    if not value:
        return False, f"Поле «{field_name}» не должно быть пустым."
    normalized = value.replace(",", ".")
    try:
        numeric_value = float(normalized)
    except ValueError:
        return False, f"Поле «{field_name}» должно быть числом."
    if not numeric_value.is_integer():
        return False, f"Поле «{field_name}» должно быть целым числом."
    int_value = int(numeric_value)
    if int_value < min_value or int_value > max_value:
        return False, f"Поле «{field_name}» должно быть в диапазоне {min_value}-{max_value}."
    return True, int_value


def _build_generation_summary(data: dict) -> str:
    return (
        "Параметры сборки:\n"
        f"Размер изображения: {_format_number(data['image_size'])}\n"
        f"Шум: {_format_number(data['noise_level'])}\n"
        f"Размер ячейки: {_format_number(data['inset_size'])}\n"
        f"Группа: {data['image_group']}"
    )


async def start_image_generation(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    await _show_generation_mode(callback.message, state)
    await callback.answer()


async def select_generation_mode(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    mode = callback.data.split(":", 1)[1]
    manual_defaults = _manual_defaults_from_preferences(
        await uow.user_repository.get_generation_preferences(callback.from_user.id)
    )
    await state.clear()
    await state.update_data(
        generation_mode=mode,
        insertion_format=settings.generation_default_insertion_format,
        alpha_channel=settings.generation_default_alpha_channel,
        manual_defaults=manual_defaults,
    )
    if mode == GENERATION_MODE_PRESETS:
        await _show_image_sizes(callback, state, uow)
        return
    await callback.message.edit_text(
        "Ручной ввод включён. Введите количество картинок по наибольшей стороне.\nРекомендуемое значение: 120",
        reply_markup=upload_image_keyboard().as_markup(),
    )
    await state.set_state(ImageGenerationStates.waiting_manual_image_size)
    await callback.answer()


async def select_image_size(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    size_value = callback.data.split("_", 2)[2]
    await state.update_data(image_size=int(float(size_value)))
    await _show_noise_levels(callback, state, uow)


async def select_noise_level(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    noise_value = callback.data.split("_", 2)[2]
    await state.update_data(noise_level=int(float(noise_value)))
    await _show_inset_sizes(callback, state, uow)


async def select_inset_size(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    inset_value = int(float(callback.data.split("_", 2)[2]))
    await state.update_data(inset_size=inset_value)
    data = await state.get_data()
    limit_error = _validate_output_side(int(data["image_size"]), int(inset_value))
    if limit_error:
        insets_result = await uow.image_build_params_repository.get_common_params(ParamType.INSET_SIZE)
        await callback.message.edit_text(
            limit_error,
            reply_markup=inset_size_keyboard(insets_result.value).as_markup(),
        )
        await state.set_state(ImageGenerationStates.selecting_inset_size)
        await callback.answer()
        return
    await _show_groups(callback, state, uow)


async def receive_manual_image_size(message: Message, state: FSMContext):
    is_valid, parsed_value = _parse_manual_int(
        message.text,
        "размер изображения",
        settings.generation_result_size_min,
        settings.generation_result_size_max,
    )
    if not is_valid:
        await message.answer(str(parsed_value), reply_markup=upload_image_keyboard().as_markup())
        return
    await state.update_data(image_size=parsed_value)
    await _ask_manual_noise_level(message, state)


async def receive_manual_noise_level(message: Message, state: FSMContext):
    is_valid, parsed_value = _parse_manual_int(
        message.text,
        "уровень шума",
        settings.generation_noise_level_min,
        settings.generation_noise_level_max,
    )
    if not is_valid:
        await message.answer(str(parsed_value), reply_markup=upload_image_keyboard().as_markup())
        return
    await state.update_data(noise_level=parsed_value)
    await _ask_manual_inset_size(message, state)


async def receive_manual_inset_size(message: Message, state: FSMContext, uow: MongoUnitOfWork):
    is_valid, parsed_value = _parse_manual_int(
        message.text,
        "размер ячейки",
        settings.generation_cell_size_min,
        settings.generation_cell_size_max,
    )
    if not is_valid:
        await message.answer(str(parsed_value), reply_markup=upload_image_keyboard().as_markup())
        return
    data = await state.get_data()
    limit_error = _validate_output_side(int(data["image_size"]), int(parsed_value))
    if limit_error:
        await message.answer(limit_error, reply_markup=upload_image_keyboard().as_markup())
        return
    await state.update_data(inset_size=parsed_value)
    await uow.user_repository.update_generation_preferences(
        message.from_user.id,
        {
            "image_size": int(data["image_size"]),
            "noise_level": int(data["noise_level"]),
            "inset_size": int(parsed_value),
        },
    )
    groups_result = await uow.image_group_repository.get_groups()
    if not groups_result.is_success or not groups_result.value:
        await message.answer("Не удалось получить группы изображений.", reply_markup=home_keyboard())
        return
    await message.answer(
        "Параметры сохранены. Теперь выберите группу изображений.",
        reply_markup=image_group_keyboard(groups_result.value).as_markup(),
    )
    await state.set_state(ImageGenerationStates.selecting_image_group)


async def select_image_group(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    group_name = callback.data.split("/", 1)[1]
    await state.update_data(image_group=group_name)
    data = await state.get_data()
    await callback.message.edit_text(
        f"{_build_generation_summary(data)}\n\nЗагрузите изображение для обработки.",
        reply_markup=upload_image_keyboard().as_markup(),
    )
    await state.set_state(ImageGenerationStates.waiting_for_image_upload)
    await callback.answer()


def _generation_job_id(message) -> str:
    # Telegram update redelivery must address the same logical generation.
    return str(uuid5(NAMESPACE_URL, f"imager:telegram:{message.chat.id}:{message.message_id}"))


async def receive_image(message: Message, state: FSMContext, uow: MongoUnitOfWork, user: User):
    if not message.photo:
        await message.answer(
            "Пожалуйста, отправьте изображение как фото.",
            reply_markup=upload_image_keyboard().as_markup(),
        )
        return

    job_id = _generation_job_id(message)
    try:
        existing = await imager_service.get_job(job_id)
        if existing is not None:
            await message.reply(
                f"Это изображение уже принято. Задание: {job_id}. Статус: {existing['status']}.",
                reply_markup=home_keyboard(),
            )
            await state.clear()
            return
    except Exception:
        exception_logger.exception("Не удалось проверить приём задания")
        await message.reply("Сервис заданий временно недоступен. Попробуйте позже.")
        return

    if not user.is_admin:
        count = await uow.generation_history_repository.count_user_records(user.telegram_id)
        if count >= settings.regular_user_upload_limit:
            await message.reply(
                f"Вы достигли лимита: {settings.regular_user_upload_limit} загрузок.",
                reply_markup=home_keyboard(),
            )
            return

    data = await state.get_data()
    limit_error = _validate_output_side(int(data["image_size"]), int(data["inset_size"]))
    if limit_error:
        await message.reply(limit_error, reply_markup=home_keyboard())
        return

    history_record_id = generation_history_record_id(job_id)
    try:
        await _set_processing_reaction(message)
        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)
        # Local files are bounded transfer buffers, never shared with the core.
        with tempfile.TemporaryDirectory(prefix="imager-upload-") as directory:
            source_path = Path(directory) / "source.jpg"
            await message.bot.download_file(file.file_path, str(source_path))
            source = await imager_service.upload_source(source_path, job_id)

        now = datetime.now(settings.tzinfo)
        history_record = GenerationHistoryRecord(
            job_id=job_id,
            telegram_id=user.telegram_id,
            telegram_username=user.telegram_username,
            queue_position=None,
            source_image_path=f"s3://{source['bucket']}/{source['key']}",
            result_image_path=None,
            image_group=data["image_group"],
            generation_mode=data.get("generation_mode", GENERATION_MODE_PRESETS),
            insertion_format=data.get("insertion_format", settings.generation_default_insertion_format),
            alpha_channel=int(data.get("alpha_channel", settings.generation_default_alpha_channel)),
            noise_level=int(data["noise_level"]),
            cell_size=int(data["inset_size"]),
            result_size=int(data["image_size"]),
            status="queued",
            created_at=now,
            updated_at=now,
        )
        request = GenerateImageRequest(
            source=source,
            group_name=data["image_group"],
            insertion_format=data.get("insertion_format", settings.generation_default_insertion_format),
            alpha_channel=int(data.get("alpha_channel", settings.generation_default_alpha_channel)),
            noise_level=int(data["noise_level"]),
            cell_size=int(data["inset_size"]),
            result_size=int(data["image_size"]),
        )
        await imager_service.submit_generation(
            request,
            job_id=job_id,
            client_context={
                "chat_id": message.chat.id,
                "reply_to_message_id": message.message_id,
                "user_id": user.telegram_id,
                "history_record_id": history_record_id,
                "display_name": f"imager_{job_id}",
                "generation_mode": data.get("generation_mode", GENERATION_MODE_PRESETS),
            },
        )
    except Exception as error:
        exception_logger.exception(f"Ошибка при приёме изображения: job_id={job_id}")
        await message.reply(
            f"Не удалось подтвердить приём задания {job_id}. {str(error)}",
            reply_markup=home_keyboard(),
        )
        return

    # The authoritative inbox is already durable. A missing projection is
    # repaired from that accepted command by the completion notifier.
    try:
        if await uow.generation_history_repository.create_record(history_record) != history_record_id:
            raise RuntimeError("Не удалось сохранить проекцию истории задания.")
    except Exception:
        exception_logger.exception(f"Задание принято, но история не сохранена: job_id={job_id}")
    await state.clear()
    await message.reply(
        f"Изображение принято. Задание: {job_id}.\n"
        "Оно будет обработано свободным воркером. Результат придёт отдельным сообщением.",
        reply_markup=build_main_menu_keyboard(user.is_admin),
    )


async def back_to_previous(callback: CallbackQuery, state: FSMContext, uow: MongoUnitOfWork):
    current_state = await state.get_state()
    state_data = await state.get_data()
    generation_mode = state_data.get("generation_mode")

    if current_state == ImageGenerationStates.choosing_generation_mode.state:
        await state.clear()
        user = await uow.user_repository.get_user_by_id(callback.from_user.id)
        is_admin = user.is_admin if user else False
        await callback.message.edit_text(MAIN_MENU_TEXT, reply_markup=build_main_menu_keyboard(is_admin))
    elif current_state == ImageGenerationStates.selecting_image_size.state:
        await _show_generation_mode(callback.message, state)
    elif current_state == ImageGenerationStates.selecting_noise_level.state:
        await _show_image_sizes(callback, state, uow)
        return
    elif current_state == ImageGenerationStates.selecting_inset_size.state:
        await _show_noise_levels(callback, state, uow)
        return
    elif current_state == ImageGenerationStates.waiting_manual_image_size.state:
        await _show_generation_mode(callback.message, state)
    elif current_state == ImageGenerationStates.waiting_manual_noise_level.state:
        await _edit_manual_image_size_prompt(callback, state)
    elif current_state == ImageGenerationStates.waiting_manual_inset_size.state:
        await _edit_manual_noise_level_prompt(callback, state)
    elif current_state == ImageGenerationStates.selecting_image_group.state:
        if generation_mode == GENERATION_MODE_MANUAL:
            await _edit_manual_inset_size_prompt(callback, state)
        else:
            await _show_inset_sizes(callback, state, uow)
            return
    elif current_state == ImageGenerationStates.waiting_for_image_upload.state:
        await _show_groups(callback, state, uow)
        return
    await callback.answer()


def register_handlers_image_generation(dp: Dispatcher, uow: MongoUnitOfWork):
    dp.callback_query.register(partial(start_image_generation, uow=uow), F.data == "generate_image")
    dp.callback_query.register(
        partial(select_generation_mode, uow=uow),
        lambda c: c.data.startswith("generation_mode:"),
    )
    dp.callback_query.register(partial(select_image_size, uow=uow), lambda c: c.data.startswith("image_size_"))
    dp.callback_query.register(partial(select_noise_level, uow=uow), lambda c: c.data.startswith("noise_level_"))
    dp.callback_query.register(partial(select_inset_size, uow=uow), lambda c: c.data.startswith("inset_size_"))
    dp.callback_query.register(partial(select_image_group, uow=uow), lambda c: c.data.startswith("image_group/"))
    dp.message.register(receive_manual_image_size, ImageGenerationStates.waiting_manual_image_size)
    dp.message.register(receive_manual_noise_level, ImageGenerationStates.waiting_manual_noise_level)
    dp.message.register(partial(receive_manual_inset_size, uow=uow), ImageGenerationStates.waiting_manual_inset_size)
    dp.message.register(partial(receive_image, uow=uow), ImageGenerationStates.waiting_for_image_upload)
    dp.callback_query.register(partial(back_to_previous, uow=uow), lambda c: c.data == "back")
