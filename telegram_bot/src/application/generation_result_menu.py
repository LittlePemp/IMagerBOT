"""Return an idle conversation to its menu after the image is delivered."""
from src.commands.main_menu import MAIN_MENU_TEXT, build_main_menu_keyboard
from src.utils.navigation import send_view


class GenerationResultMenu:
    def __init__(self, dispatcher, users, bot, *, fixed_admin_ids=()):
        self.dispatcher, self.users, self.bot = dispatcher, users, bot
        self.fixed_admin_ids = frozenset(fixed_admin_ids)

    async def __call__(self, job):
        context = job['request']['client_context']
        actor_id, chat_id = context.get('user_id'), context.get('chat_id')
        if (not isinstance(actor_id, int) or isinstance(actor_id, bool)
                or not isinstance(chat_id, int) or isinstance(chat_id, bool)
                or actor_id <= 0 or actor_id != chat_id):
            return False
        actor_id, chat_id = int(actor_id), int(chat_id)
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=chat_id, user_id=actor_id)
        # This is the same per-conversation lock used by incoming updates.
        # A late result must not replace the keyboard of a new form.
        async with self.dispatcher.fsm.events_isolation.lock(key=state.key):
            if await state.get_state() is not None or (await state.get_data()).get('draft_id'):
                return False
            user = await self.users.get_user_by_id(actor_id)
            fixed_admin = actor_id in self.fixed_admin_ids
            if user is None or (user.isbanned and not fixed_admin):
                return False
            await send_view(
                self.bot, state, chat_id, MAIN_MENU_TEXT,
                build_main_menu_keyboard(user.is_admin or fixed_admin), owner_id=actor_id,
            )
        return True
