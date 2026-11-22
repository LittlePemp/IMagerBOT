"""A separate support dispatcher; media here can never enter image generation."""
from aiogram import BaseMiddleware, Dispatcher, F
from aiogram.filters import Command, Filter

from src.utils.middlewares.abuse_control import AbuseControlMiddleware
from src.utils.middlewares.authentication_middleware import AuthenticationMiddleware
from src.utils.middlewares.errors import ErrorBoundaryMiddleware


async def start_support(message, user, support_service):
    await support_service.start(message, user)


async def receive_support_message(message, user, support_service):
    if await support_service.is_admin(user.telegram_id):
        await support_service.accept_admin_reply(message, user)
    else:
        await support_service.accept_user_message(message, user)


def create_support_dispatcher(users, service):
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher['support_service'] = service
    dispatcher.update.outer_middleware(ErrorBoundaryMiddleware())
    dispatcher.update.outer_middleware(AbuseControlMiddleware())
    dispatcher.message.filter(F.chat.type == 'private')
    dispatcher.message.outer_middleware(AuthenticationMiddleware(users))
    dispatcher.message.register(start_support, Command('start', 'help', 'menu'))
    dispatcher.message.register(receive_support_message)
    return dispatcher


class MainSupportReply(Filter):
    def __init__(self, service):
        self.service = service

    async def __call__(self, message):
        original = message.reply_to_message
        if (message.chat.type != 'private' or message.from_user is None
                or message.chat.id != message.from_user.id or original is None
                or original.from_user is None or not original.from_user.is_bot
                or message.bot.id != self.service.main_bot.id
                or original.from_user.id != self.service.main_bot.id):
            return False
        if not await self.service.is_admin(message.from_user.id):
            return False
        return bool(await self.service.repository.find_main_alert(
            message.from_user.id, original.message_id,
        ))


class MainSupportReplyGuard(BaseMiddleware):
    def __init__(self, service):
        self.service, self.matches = service, MainSupportReply(service)

    async def __call__(self, handler, message, data):
        if await self.matches(message):
            # Run before conversation recovery and generation routing, so a
            # staff screenshot reply can never become a mosaic input.
            await self.service.redirect_main_reply(message)
            return
        return await handler(message, data)


def register_main_support_reply_guard(dispatcher, service):
    dispatcher['support_service'] = service
    dispatcher.message.outer_middleware(MainSupportReplyGuard(service))
