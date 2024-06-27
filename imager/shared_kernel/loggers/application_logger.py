from loguru import logger

logger.add(
    'logs/application_{time}.log',
    rotation='1 week',
    retention='1 month',
    level='DEBUG',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message}',
    enqueue=True
)

application_logger = logger.bind(name='application_logger')
