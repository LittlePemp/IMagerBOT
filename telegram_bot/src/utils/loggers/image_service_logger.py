from loguru import logger

logger.add(
    'logs/image_service_{time}.log',
    rotation='1 week',
    retention='1 month',
    level='ERROR',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    enqueue=True
)

image_service_logger = logger.bind(name='image_service_logger')
