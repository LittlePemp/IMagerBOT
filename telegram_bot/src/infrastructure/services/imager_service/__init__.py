from settings import settings
from .imager_service import ImagerService


imager_service = ImagerService(settings.imager_service_url)
