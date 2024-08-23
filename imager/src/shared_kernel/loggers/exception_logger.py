from loguru import logger

logger.add(
    'logs/exception.log',
    rotation='1 week',
    retention='1 month',
    level='ERROR',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message} | {exception}',
    enqueue=True,
    filter=lambda record: record['extra'].get('logger_name') == 'exception_logger'
)

exception_logger = logger.bind(logger_name='exception_logger')
