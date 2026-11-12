from loguru import logger
from imager_config.loader import config
from imager_contracts.protocol import LOG_EXCEPTION_LOCALS

logger.add(
    'logs/services.log',
    rotation=config.logging.rotation_bytes,
    retention=config.logging.retained_archives,
    level='INFO',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    filter=lambda record: record['extra'].get('name') == 'service_logger',
    enqueue=True,
    diagnose=LOG_EXCEPTION_LOCALS,
)

service_logger = logger.bind(name='service_logger')
