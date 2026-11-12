from loguru import logger
from imager_config.loader import config

logger.add(
    'logs/application.log',
    rotation=config.logging.rotation_bytes,
    retention=config.logging.retained_archives,
    level='DEBUG',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    enqueue=True,
    filter=lambda record: record['extra'].get('logger_name') == 'application_logger'
)

application_logger = logger.bind(logger_name='application_logger')
