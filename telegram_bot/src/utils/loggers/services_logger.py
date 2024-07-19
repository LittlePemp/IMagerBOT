from loguru import logger

logger.add(
    'logs/services_{time}.log',
    rotation='1 week',
    retention='1 month',
    level='DEBUG',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    enqueue=True
)

service_logger = logger.bind(name='service_logger')
