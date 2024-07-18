from aiogram import types
from aiogram import BaseMiddleware


class LoggingMiddleware(BaseMiddleware):
    def __init__(self, logger):
        super(LoggingMiddleware, self).__init__()
        self.logger = logger

    async def __call__(self, handler, event, data):
        self.logger.info(f'Received event: {event}')
        result = await handler(event, data)
        self.logger.info(f'Processed event: {event} with result: {result}')
        return result
