from loguru import logger

logger.add(
    'logs/database.log',
    rotation='1 week',
    retention='1 month',
    level='DEBUG',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    filter=lambda record: record['extra'].get('name') == 'db_logger',
    enqueue=True
)

db_logger = logger.bind(name='db_logger')
