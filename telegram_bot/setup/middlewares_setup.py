from aiogram import Dispatcher
from src.utils.middlewares import logging_middleware
from src.utils.middlewares.authentication_middleware import \
    AuthenticationMiddleware


def setup_middlewares(dp: Dispatcher, uow):
    dp.update.outer_middleware(logging_middleware)
    dp.message.middleware(AuthenticationMiddleware(uow.user_repository))
