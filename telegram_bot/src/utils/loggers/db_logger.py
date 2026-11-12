from loguru import logger
from imager_config.loader import config
from imager_contracts.protocol import LOG_EXCEPTION_LOCALS

logger.add(
    'logs/database.log',
    rotation=config.logging.rotation_bytes,
    retention=config.logging.retained_archives,
    level='DEBUG',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    filter=lambda record: record['extra'].get('name') == 'db_logger',
    enqueue=True,
    diagnose=LOG_EXCEPTION_LOCALS,
)

db_logger = logger.bind(name='db_logger')
