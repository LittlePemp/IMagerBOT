import sys
from loguru import logger
from imager_contracts.protocol import LOG_EXCEPTION_LOCALS

logger.remove()
logger.add(sys.stderr, diagnose=LOG_EXCEPTION_LOCALS)

from .bot_requests_logger import bot_requests_logger  # noqa
from .db_logger import db_logger  # noqa
from .exception_logger import exception_logger  # noqa
from .services_logger import service_logger  # noqa
