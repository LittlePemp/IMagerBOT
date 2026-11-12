from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError

from src.utils.loggers import exception_logger


class ErrorBoundaryMiddleware(BaseMiddleware):
    async def __call__(self, handler, update, data):
        try:
            return await handler(update, data)
        except Exception:
            exception_logger.exception(f'Bot update failed: update_id={update.update_id}')
            event = update.callback_query or update.message
            if event is not None:
                try:
                    await event.answer('Не удалось выполнить действие. Повторите позже или откройте /menu.')
                except TelegramAPIError:
                    exception_logger.warning('Could not deliver update failure notice')
