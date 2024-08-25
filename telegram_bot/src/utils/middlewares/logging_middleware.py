import traceback

from aiogram.dispatcher.middlewares.base import BaseMiddleware


class LoggingMiddleware(BaseMiddleware):
    def __init__(self, logger):
        super(LoggingMiddleware, self).__init__()
        self.logger = logger

    async def __call__(self, handler, event, data):
        user_id = getattr(event.from_user, 'id', 'unknown_user')
        event_type = type(event).__name__

        self.logger.info(
            f'Received event: {event_type} from user {user_id}'
        )

        try:
            result = await handler(event, data)
            self.logger.info(
                f'Processed event: {event_type} from user {user_id} with result: {result}'
            )
            return result
        except Exception as e:
            self.logger.error(
                f'Error processing event: {event_type} from user {user_id}. Error: {e}',
                extra={
                    'traceback': traceback.format_exc()
                }
            )
            raise
