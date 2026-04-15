from settings import settings
from .imager_service import ImagerService


imager_service = ImagerService(
    settings.rabbitmq_url,
    settings.rabbitmq_requests_queue,
)
