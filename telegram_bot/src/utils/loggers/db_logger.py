from loguru import logger

logger.add(
    'logs/database.log',
    rotation='10 MB',
    retention=3,
    level='DEBUG',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    filter=lambda record: record['extra'].get('name') == 'db_logger',
    enqueue=True
)

db_logger = logger.bind(name='db_logger')
