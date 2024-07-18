
from.logging_middleware import LoggingMiddleware
from src.utils.loggers import bot_requests_logger

logging_middleware = LoggingMiddleware(bot_requests_logger)
