from loguru import logger
from imager_config.loader import config
from imager_contracts.protocol import LOG_EXCEPTION_LOCALS

logger.add(
    'logs/exceptions.log',
    rotation=config.logging.rotation_bytes,
    retention=config.logging.retained_archives,
    level='ERROR',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message} | {exception}',
    filter=lambda record: record['extra'].get('name') == 'exception_logger',
    enqueue=True,
    diagnose=LOG_EXCEPTION_LOCALS,
)

exception_logger = logger.bind(name='exception_logger')
