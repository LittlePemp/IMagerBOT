"""Telegram source transfer and the small photo handler."""
import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from imager_config.loader import config
from imager_contracts.protocol import TELEGRAM_JOB_ID_NAMESPACE
from src.commands.common_keyboards import home_keyboard
from src.infrastructure.data.errors import AccessDenied, AdmissionUncertain, DailyQuotaExceeded
from src.utils.middlewares.abuse_control import UploadBusy
from src.utils.navigation import render_view
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
    try:
        receipt = await generation_service.execute(
            actor_id=user.telegram_id, draft=draft, job_id=generation_job_id(message),
            client_context={
                'chat_id': message.chat.id, 'reply_to_message_id': message.message_id,
                'generation_mode': draft.get('generation_mode', 'presets'),
                'display_name': f'{config.render.output_stem}_{generation_job_id(message)}',
            }, source_loader=TelegramPhotoSource(message, generation_service.gateway),
        )
    except DailyQuotaExceeded as error:
        await message.answer(f'На сегодня доступно {error.limit} изображений. Лимит обновится в полночь ({config.bot.timezone}).')
        return
    except AdmissionUncertain as error:
        await message.answer(f'Приём задания {error.job_id} уточняется. Повторная доставка этого сообщения не спишет лимит ещё раз.')
        return
    except (AccessDenied, UploadBusy, ValueError) as error:
        await render_view(message, state, str(error), home_keyboard())
        return
    await state.clear()
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text='Статус задания', callback_data=f'job_status:{receipt.job_id}')
    keyboard.button(text='Главное меню', callback_data='main_menu')
    keyboard.adjust(1)
    await render_view(
        message, state,
        f'Изображение {"уже " if receipt.duplicate else ""}принято. Задание: {receipt.job_id}.\n'
        'Результат придёт отдельным сообщением. Повтор этого задания не расходует лимит.',
        keyboard.as_markup(),
    )
