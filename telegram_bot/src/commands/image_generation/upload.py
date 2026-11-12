"""Telegram source transfer and the small photo handler."""
import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from imager_config.loader import config
from imager_contracts.protocol import TELEGRAM_JOB_ID_NAMESPACE
from src.commands.common_keyboards import home_keyboard
from src.infrastructure.data.errors import AccessDenied, AdmissionUncertain, DailyQuotaExceeded
from src.infrastructure.services.telegram_files import validate_download_path
from src.utils.middlewares.abuse_control import UploadBusy
from src.utils.navigation import render_view
from src.utils.result_files import contains_generation_reference, new_result_filename
from aiogram.utils.keyboard import InlineKeyboardBuilder


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


class TelegramPhotoSource:
    def __init__(self, message, gateway):
        self.message, self.gateway = message, gateway

    async def __call__(self, job_id):
        photo = self.message.photo[-1]
        remote = await self.message.bot.get_file(photo.file_id)
        validate_download_path(self.message.bot, remote)
        if remote.file_size is not None and remote.file_size > config.limits.source_file_bytes:
            raise ValueError('Размер исходного файла превышает допустимый предел.')
        with tempfile.TemporaryDirectory(prefix=config.storage.upload_temp_prefix) as directory:
            path = Path(directory) / config.storage.upload_filename
            with path.open('wb+') as stream:
                await self.message.bot.download_file(
                    remote.file_path, BoundedWriter(stream, config.limits.source_file_bytes),
                    timeout=config.bot.download_timeout_seconds,
                )
            return await self.gateway.upload_source(path, job_id)


async def receive_image(message, state, user, generation_service):
    draft = await state.get_data()
    job_id = generation_job_id(message)
    try:
        receipt = await generation_service.execute(
            actor_id=user.telegram_id, draft=draft, job_id=job_id,
            client_context={
                'chat_id': message.chat.id, 'reply_to_message_id': message.message_id,
                'generation_mode': draft.get('generation_mode', 'presets'),
                'display_name': new_result_filename(getattr(user, 'telegram_username', None)),
            }, source_loader=TelegramPhotoSource(message, generation_service.gateway),
        )
    except DailyQuotaExceeded as error:
        await message.answer(f'На сегодня доступно {error.limit} изображений. Лимит обновится в полночь ({config.bot.timezone}).')
        return
    except AdmissionUncertain as error:
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text='Проверить статус', callback_data=f'job_status:{error.job_id}')
        keyboard.button(text='Главное меню', callback_data='main_menu')
        keyboard.adjust(1)
        await render_view(
            message, state,
            'Пока не удалось подтвердить приём изображения. Проверьте статус немного позже.',
            keyboard.as_markup(),
        )
        return
    except (AccessDenied, UploadBusy, ValueError) as error:
        text = str(error)
        if contains_generation_reference(text, job_id=job_id) or 'задан' in text.casefold():
            text = 'Не удалось принять изображение. Проверьте параметры и попробуйте снова.'
        await render_view(message, state, text, home_keyboard())
        return
    await state.clear()
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text='Статус изображения', callback_data=f'job_status:{receipt.job_id}')
    keyboard.button(text='Главное меню', callback_data='main_menu')
    keyboard.adjust(1)
    await render_view(
        message, state,
        f'Изображение {"уже " if receipt.duplicate else ""}принято. '
        'Результат придёт отдельным сообщением.',
        keyboard.as_markup(),
    )
