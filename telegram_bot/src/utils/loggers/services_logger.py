from loguru import logger

logger.add(
    'logs/services_{time}.log',
    rotation='1 week',
    retention='1 month',
    level='DEBUG',
    format='{time:YYYY-MM-DD at HH:mm:ss} | {level} | {message} | {extra[service]}',
    enqueue=True
)

def get_service_logger(service_name):
    return logger.bind(service=service_name)
