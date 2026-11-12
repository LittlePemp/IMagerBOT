from loguru import logger
from imager_config.loader import config
from imager_contracts.protocol import LOG_EXCEPTION_LOCALS

logger.add(
    'logs/database.log',
    rotation=config.logging.rotation_bytes,
    retention=config.logging.retained_archives,
    level='DEBUG',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    enqueue=True,
    diagnose=LOG_EXCEPTION_LOCALS,
    filter=lambda record: record['extra'].get('logger_name') == 'db_logger'
)

db_logger = logger.bind(logger_name='db_logger')
