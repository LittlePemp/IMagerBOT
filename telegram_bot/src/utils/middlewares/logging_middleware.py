from aiogram.dispatcher.middlewares.base import BaseMiddleware

class LoggingMiddleware(BaseMiddleware):
    def __init__(self, logger):
        super(LoggingMiddleware, self).__init__()
        self.logger = logger

    async def __call__(self, handler, event, data):
        data['service'] = data.get('service', 'unknown_service')
        self.logger.info(f'Received update: update_id={event.update_id}')
        try:
            result = await handler(event, data)
            self.logger.info(f'Processed update: update_id={event.update_id}')
            return result
        except Exception as e:
            self.logger.exception(f'Update failed: update_id={event.update_id}')
            raise
