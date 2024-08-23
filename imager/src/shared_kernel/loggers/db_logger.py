from loguru import logger

logger.add(
    'logs/database.log',
    rotation='1 week',
    retention='1 month',
    level='DEBUG',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    enqueue=True,
    filter=lambda record: record['extra'].get('logger_name') == 'db_logger'
)

db_logger = logger.bind(logger_name='db_logger')
