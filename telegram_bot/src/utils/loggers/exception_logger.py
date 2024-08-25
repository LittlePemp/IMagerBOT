from loguru import logger

logger.add(
    'logs/exceptions.log',
    rotation='1 week',
    retention='1 month',
    level='ERROR',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message} | {exception}',
    filter=lambda record: record['extra'].get('name') == 'exception_logger',
    enqueue=True
)

exception_logger = logger.bind(name='exception_logger')
