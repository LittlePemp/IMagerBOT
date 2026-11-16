"""A current form survives inactivity; a deleted form recovers without executing old actions."""
import unittest
from datetime import datetime, timezone

from aiogram.methods import AnswerCallbackQuery, SendMessage

from src.commands.image_generation.image_generation_states import ImageGenerationStates as S
from src.utils.navigation import CURRENT_VIEW, STALE_VIEW_TEXT
import test_bot_routes as routes


class PersistentConversationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = routes.BotRouteTests.asyncSetUp
    asyncTearDown = routes.BotRouteTests.asyncTearDown
    actor = routes.BotRouteTests.actor
    send = routes.BotRouteTests.send
    click = routes.BotRouteTests.click
    state = routes.BotRouteTests.state

    async def legacy_expiry(self, actor_id):
        view = (await self.state(actor_id).get_data())[CURRENT_VIEW]
        view.update(expires_at=1, expires_on=datetime(1970, 1, 1, tzinfo=timezone.utc))
        await self.state(actor_id).update_data(**{CURRENT_VIEW: view})

    async def test_current_user_and_admin_callbacks_keep_working_after_old_deadline(self):
        await self.send(3, '/menu')
        await self.legacy_expiry(3)
        await self.click(3, title='Собрать изображение')
        self.assertEqual(await self.state(3).get_state(), S.choosing_generation_mode.state)
        await self.send(999, '/admin')
        await self.legacy_expiry(999)
        await self.click(999, title='Пользователи')
        self.users.get_user_count.assert_awaited_once()
        self.assertFalse(any(isinstance(request, AnswerCallbackQuery) and request.text == STALE_VIEW_TEXT
                             for request in self.session.methods))

    async def test_manual_form_continues_instead_of_rejecting_next_day_input(self):
        await self.send(3, '/menu')
        await self.click(3, title='Собрать изображение')
        await self.click(3, title='Вручную')
        await self.legacy_expiry(3)
        await self.send(3, '64')
        self.assertEqual(await self.state(3).get_state(), S.waiting_manual_noise_level.state)
        self.assertEqual((await self.state(3).get_data())['image_size'], 64)

    async def test_deleted_user_or_admin_view_opens_bound_menu_without_running_old_action(self):
        for actor_id, command, title in ((3, '/menu', 'Собрать изображение'),
                                         (999, '/admin', 'Пользователи')):
            with self.subTest(actor=actor_id):
                menu = await self.send(actor_id, command)
                await self.state(actor_id).clear()
                fresh = await self.click(actor_id, title=title, message=menu)
                self.assertEqual(fresh.text, 'Главное меню')
                self.assertNotEqual(fresh.message_id, menu.message_id)
                self.assertIsNone(await self.state(actor_id).get_state())
                view = (await self.state(actor_id).get_data())[CURRENT_VIEW]
                self.assertEqual((view['owner_id'], view['message_id']), (actor_id, fresh.message_id))
                await self.click(actor_id, title='Собрать изображение', message=fresh)
                self.assertEqual(await self.state(actor_id).get_state(), S.choosing_generation_mode.state)
        self.users.get_user_count.assert_not_awaited()

    async def test_superseded_button_does_not_reset_a_new_form(self):
        old = await self.send(3, '/menu')
        await self.click(3, title='Собрать изображение')
        await self.click(3, title='Вручную')
        before = await self.state(3).get_data()
        await self.click(3, title='Собрать изображение', message=old)
        self.assertEqual(await self.state(3).get_state(), S.waiting_manual_image_size.state)
        self.assertEqual(await self.state(3).get_data(), before)
        self.assertEqual(self.session.methods[-1].text, STALE_VIEW_TEXT)

    async def test_another_users_button_does_not_trigger_recovery(self):
        menu = await self.send(999, '/admin')
        sent_before = sum(isinstance(request, SendMessage) for request in self.session.methods)
        await self.click(3, title='Пользователи', message=menu)
        self.assertEqual(sum(isinstance(request, SendMessage) for request in self.session.methods), sent_before)
        self.assertEqual(await self.state(3).get_data(), {})
        self.users.get_user_count.assert_not_awaited()

    async def test_active_state_without_its_view_recovers_once_instead_of_repeated_errors(self):
        await self.state(3).set_state(S.waiting_manual_noise_level)
        await self.state(3).set_data({'image_size': 64})
        fresh = await self.send(3, '30')
        self.assertEqual(fresh.text, 'Главное меню')
        self.assertIsNone(await self.state(3).get_state())
        await self.click(3, title='Собрать изображение')
        self.assertEqual(await self.state(3).get_state(), S.choosing_generation_mode.state)

    async def test_text_after_entire_conversation_was_deleted_opens_a_fresh_menu(self):
        await self.state(3).clear()
        fresh = await self.send(3, '30')
        self.assertEqual(fresh.text, 'Главное меню')
        await self.click(3, title='Собрать изображение', message=fresh)
        self.assertEqual(await self.state(3).get_state(), S.choosing_generation_mode.state)
