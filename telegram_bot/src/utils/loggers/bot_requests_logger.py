from loguru import logger

logger.add(
    'logs/bot_requests_{time}.log',
    rotation='1 week',
    retention='1 month',
    level='ERROR',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    enqueue=True
)

bot_requests_logger = logger.bind(name='bot_requests_logger')
