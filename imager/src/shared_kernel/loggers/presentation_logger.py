from loguru import logger

logger.add(
    'logs/presentation.log',
    rotation='1 week',
    retention='1 month',
    level='DEBUG',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message} | {exception}',
    enqueue=True,
    filter=lambda record: record['extra'].get('logger_name') == 'presentation_logger'
)

presentation_logger = logger.bind(logger_name='presentation_logger')
