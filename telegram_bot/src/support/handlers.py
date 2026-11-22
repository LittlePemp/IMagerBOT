"""A separate support dispatcher; media here can never enter image generation."""
from dataclasses import replace

from aiogram import Dispatcher, F
from aiogram.filters import Command

from imager_config.loader import config
from src.utils.middlewares.abuse_control import AbuseControlMiddleware, RequestLimiter
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
    dispatcher.update.outer_middleware(AbuseControlMiddleware(RequestLimiter(replace(
        config.bot, throttle_notice_seconds=service.policy.notice_cooldown_seconds,
    ))))
    dispatcher.message.filter(F.chat.type == 'private')
    dispatcher.message.outer_middleware(AuthenticationMiddleware(users))
    dispatcher.message.register(start_support, Command('start', 'help', 'menu'))
    dispatcher.message.register(receive_support_message)
    return dispatcher
