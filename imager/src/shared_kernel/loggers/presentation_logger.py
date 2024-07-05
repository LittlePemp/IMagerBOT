from loguru import logger

logger.add(
    'logs/presentation_{time}.log',
    rotation='1 week',
    retention='1 month',
    level='DEBUG',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message} | {exception}',
    enqueue=True
)

presentation_logger = logger.bind(name='presentation_logger')
