from loguru import logger

logger.add(
    'logs/services.log',
    rotation='1 week',
    retention='1 month',
    level='INFO',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    filter=lambda record: record['extra'].get('name') == 'service_logger',
    enqueue=True
)

service_logger = logger.bind(name='service_logger')
