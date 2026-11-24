"""Bounded Telegram photo/sticker transfer and source admission."""
import tempfile
import shutil
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from imager_config.loader import config
from imager_contracts.protocol import (
    BINARY_CONTENT_TYPE, JPEG_CONTENT_TYPE, WEBP_CONTENT_TYPE, TELEGRAM_JOB_ID_NAMESPACE,
)
from src.application.image_errors import image_aspect_error
from src.application.generation import GenerationSettingsUnavailable
from src.commands.common_keyboards import home_keyboard
from src.infrastructure.data.errors import AccessDenied, AdmissionUncertain, DailyQuotaExceeded
from src.infrastructure.services.telegram_files import release_local_file, validate_download_path
from src.utils.middlewares.abuse_control import UploadBusy
from src.utils.navigation import render_view
from src.utils.result_files import contains_generation_reference, new_result_filename


def generation_job_id(message):
    return str(uuid5(NAMESPACE_URL, f'{TELEGRAM_JOB_ID_NAMESPACE}{message.chat.id}:{message.message_id}'))


class BoundedWriter:
    def __init__(self, stream, maximum):
        self.stream, self.maximum, self.written = stream, maximum, 0

    def write(self, chunk):
        if self.written + len(chunk) > self.maximum:
            raise ValueError('Размер исходного файла превышает допустимый предел.')
        self.written += len(chunk)
        return self.stream.write(chunk)

    def flush(self):
        return self.stream.flush()

    def seek(self, *args):
        return self.stream.seek(*args)


MISSING_STICKER_PREVIEW_TEXT = (
    'У этого стикера нет превью для сборки. '
    'Пришлите другой стикер или фото.'
)


class TelegramImageSource:
    def __init__(self, message, gateway):
        self.message, self.gateway = message, gateway

    def descriptor(self):
        sticker = getattr(self.message, 'sticker', None)
        if sticker is not None:
            if sticker.is_animated or sticker.is_video:
                if sticker.thumbnail is None:
                    raise ValueError(MISSING_STICKER_PREVIEW_TEXT)
                # Telegram renders the static preview. The bot transfers it;
                # decoding and generation still belong to the core.
                return sticker.thumbnail, BINARY_CONTENT_TYPE
            return sticker, WEBP_CONTENT_TYPE
        photos = getattr(self.message, 'photo', None)
        if photos:
            return photos[-1], JPEG_CONTENT_TYPE
        raise ValueError('Отправьте фото или стикер.')

    def validate(self):
        image, content_type = self.descriptor()
        error = image_aspect_error(getattr(image, 'width', None), getattr(image, 'height', None))
        if error:
            raise ValueError(error)
        if image.file_size is not None and image.file_size > config.limits.source_file_bytes:
            raise ValueError('Размер исходного файла превышает допустимый предел.')
        return image, content_type

    async def __call__(self, job_id):
        image, content_type = self.validate()
        self._check_disk()
        remote = await self.message.bot.get_file(image.file_id)
        validate_download_path(self.message.bot, remote)
        if remote.file_size is not None and remote.file_size > config.limits.source_file_bytes:
            raise ValueError('Размер исходного файла превышает допустимый предел.')
        self._check_disk()
        with tempfile.TemporaryDirectory(prefix=config.storage.upload_temp_prefix) as directory:
            path = Path(directory) / config.storage.upload_filename
            with path.open('wb+') as stream:
                await self.message.bot.download_file(
                    remote.file_path, BoundedWriter(stream, config.limits.source_file_bytes),
                    timeout=config.bot.download_timeout_seconds,
                )
            reference = await self.gateway.upload_source(path, job_id, content_type=content_type)
        # The Local Bot API keeps every downloaded file forever. MinIO now owns
        # the bytes, so the cached copy is released right away.
        release_local_file(self.message.bot, remote.file_path)
        return reference

    def _check_disk(self):
        roots = {Path(tempfile.gettempdir())}
        api = getattr(getattr(self.message.bot, 'session', None), 'api', None)
        if getattr(api, 'is_local', False):
            roots.add(Path(config.pack_import.bot_api_container_directory))
        required = (config.bot.source_disk_headroom_bytes
                    + config.limits.source_file_bytes * config.bot.max_concurrent_uploads)
        if any(shutil.disk_usage(root).free < required for root in roots):
            raise ValueError('Недостаточно свободного места для загрузки изображения. Попробуйте позже.')


# Compatibility for the existing adapter callers; the implementation accepts
# both supported Telegram media kinds through one admission boundary.
TelegramPhotoSource = TelegramImageSource


async def receive_image(message, state, user, generation_service, generation_progress):
    await _receive_image(
        message, state, user, generation_service, generation_progress, draft=await state.get_data(),
    )


async def receive_latest_image(message, state, user, generation_service, generation_progress):
    await _receive_image(message, state, user, generation_service, generation_progress, draft=None)


async def _receive_image(message, state, user, generation_service, generation_progress, *, draft):
    job_id = generation_job_id(message)
    try:
        source = TelegramImageSource(message, generation_service.gateway)
        source.validate()
        receipt = await generation_service.execute(
            actor_id=user.telegram_id, draft=draft, job_id=job_id,
            client_context={
                'chat_id': message.chat.id, 'reply_to_message_id': message.message_id,
                'generation_mode': draft.get('generation_mode', 'presets') if draft is not None else 'repeat',
                'display_name': new_result_filename(getattr(user, 'telegram_username', None)),
            }, source_loader=source,
        )
    except DailyQuotaExceeded as error:
        await message.answer(f'На сегодня доступно {error.limit} изображений. Лимит обновится в полночь ({config.bot.timezone}).')
        return
    except AdmissionUncertain as error:
        await state.clear()
        await generation_progress.watch(message, job_id=error.job_id, actor_id=user.telegram_id)
        return
    except GenerationSettingsUnavailable as error:
        from .wizard import start
        await start(message, state, prompt=f'{error}\nВыберите настройки, затем отправьте фото или стикер.')
        return
    except (AccessDenied, UploadBusy, ValueError) as error:
        text = str(error)
        if contains_generation_reference(text, job_id=job_id) or 'задан' in text.casefold():
            text = 'Не удалось принять изображение. Проверьте параметры и попробуйте снова.'
        await render_view(message, state, text, home_keyboard())
        return
    # A replay can refer to an older image while a new form is already open.
    # Only a newly accepted image completes the current conversation.
    if not receipt.duplicate:
        await state.clear()
    await generation_progress.watch(
        message, job_id=receipt.job_id, actor_id=user.telegram_id, duplicate=receipt.duplicate,
    )
