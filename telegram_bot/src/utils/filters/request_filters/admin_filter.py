from aiogram.filters import BaseFilter
from settings import settings


class AdminFilter(BaseFilter):
    async def __call__(self, event, user=None, user_repository=None):
        actor_id = event.from_user.id
        if actor_id in settings.telegram_admin_ids:
            return True
        if user_repository is not None:
            user = await user_repository.get_user_by_id(actor_id)
        return bool(user is not None and user.is_admin and not user.isbanned)
