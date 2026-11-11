from loguru import logger

logger.add(
    'logs/exception.log',
    rotation='10 MB',
    retention=3,
    level='ERROR',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message} | {exception}',
    enqueue=True,
    filter=lambda record: record['extra'].get('logger_name') == 'exception_logger'
)

exception_logger = logger.bind(logger_name='exception_logger')
