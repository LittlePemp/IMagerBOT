from loguru import logger
from imager_config.loader import config

logger.add(
    'logs/bot_requests.log',
    rotation=config.logging.rotation_bytes,
    retention=config.logging.retained_archives,
    level='INFO',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    filter=lambda record: record['extra'].get('name') == 'bot_requests_logger',
    enqueue=True
)

bot_requests_logger = logger.bind(name='bot_requests_logger')
