from aiogram.dispatcher.middlewares.base import BaseMiddleware

class LoggingMiddleware(BaseMiddleware):
    def __init__(self, logger):
        super(LoggingMiddleware, self).__init__()
        self.logger = logger

    async def __call__(self, handler, event, data):
        data['service'] = data.get('service', 'unknown_service')
        self.logger.info(f'Received event: {event}', extra={'service': data['service']})
        result = await handler(event, data)
        self.logger.info(f'Processed event: {event} with result: {result}', extra={'service': data['service']})
        return result
