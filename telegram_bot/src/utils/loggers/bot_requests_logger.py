from loguru import logger

logger.add(
    'logs/bot_requests.log',
    rotation='10 MB',
    retention=3,
    level='INFO',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    filter=lambda record: record['extra'].get('name') == 'bot_requests_logger',
    enqueue=True
)

bot_requests_logger = logger.bind(name='bot_requests_logger')
