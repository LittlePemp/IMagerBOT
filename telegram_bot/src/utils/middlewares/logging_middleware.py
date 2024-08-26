import traceback

from aiogram.dispatcher.middlewares.base import BaseMiddleware


class LoggingMiddleware(BaseMiddleware):
    def __init__(self, logger):
        super(LoggingMiddleware, self).__init__()
        self.logger = logger

    async def __call__(self, handler, event, data):
        print(event, dir(event))
        user_id = 'unknown_user'
        if hasattr(event, 'message') and event.message:
            user_id = event.message.from_user.id
        elif hasattr(event, 'callback_query') and event.callback_query:
            user_id = event.callback_query.from_user.id
        elif hasattr(event, 'inline_query') and event.inline_query:
            user_id = event.inline_query.from_user.id
        elif hasattr(event, 'chat_member') and event.chat_member:
            user_id = event.chat_member.from_user.id
        elif hasattr(event, 'my_chat_member') and event.my_chat_member:
            user_id = event.my_chat_member.from_user.id

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
