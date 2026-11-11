from loguru import logger

logger.add(
    'logs/application.log',
    rotation='10 MB',
    retention=3,
    level='DEBUG',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    enqueue=True,
    filter=lambda record: record['extra'].get('logger_name') == 'application_logger'
)

application_logger = logger.bind(logger_name='application_logger')
