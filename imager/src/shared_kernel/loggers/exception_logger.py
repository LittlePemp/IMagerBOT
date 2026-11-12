from loguru import logger
from imager_config.loader import config
from imager_contracts.protocol import LOG_EXCEPTION_LOCALS

logger.add(
    'logs/exception.log',
    rotation=config.logging.rotation_bytes,
    retention=config.logging.retained_archives,
    level='ERROR',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message} | {exception}',
    enqueue=True,
    diagnose=LOG_EXCEPTION_LOCALS,
    filter=lambda record: record['extra'].get('logger_name') == 'exception_logger'
)

exception_logger = logger.bind(logger_name='exception_logger')
