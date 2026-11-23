"""Exercise the real Dispatcher with a Telegram session that cannot use network."""
import importlib
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TelegramUser

from src.commands.admin_panel.image_build_params.common_params.common_params_states import CommonParamsStates
from src.commands.image_generation.image_generation_states import ImageGenerationStates as GenerationStates
from src.commands.image_generation.upload import generation_job_id
from src.models.image_build_params import CommonParam, ParamType
from src.models.image_group import ImageGroup
from src.models.user import User, UserStatus
from src.utils.building_blocks.result import Result
from src.utils.middlewares.abuse_control import AbuseControlMiddleware


class FakeTelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.methods = []
        self.messages = {}
        self.last = {}
        self.next_message_id = 100
        self.unsupported = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise AssertionError('Route tests must not download files')
        yield b''

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, EditMessageText)):
            chat_id = int(method.chat_id)
            if isinstance(method, SendMessage):
                self.next_message_id += 1
                message_id = self.next_message_id
            else:
                message_id = method.message_id
            result = Message(
                message_id=message_id, date=datetime.now(timezone.utc),
                chat=Chat(id=chat_id, type='private'),
                from_user=TelegramUser(id=bot.id, is_bot=True, first_name='IMager'),
                text=method.text, reply_markup=method.reply_markup,
            ).as_(bot)
            self.messages[(chat_id, message_id)] = result
            self.last[chat_id] = result
            return result
        self.unsupported.append(type(method).__name__)
        raise AssertionError(f'Unexpected Telegram operation: {type(method).__name__}')


class BotRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = FakeTelegramSession()
        self.bot = Bot(token='123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=self.session)
        now = datetime.now(timezone.utc)
        self.people = {
            actor_id: User(
                telegram_id=actor_id, telegram_username=None, name='Тест',
                registered_datetime_utc=now, last_activity_datetime_utc=now,
                isbanned=False, status=status,
            ) for actor_id, status in ((999, UserStatus.USER), (2, UserStatus.ADMIN), (3, UserStatus.USER))
        }
        self.users = SimpleNamespace(
            get_user_by_id=AsyncMock(side_effect=lambda actor_id: self.people.get(actor_id)),
            update_user_info=AsyncMock(return_value=True),
            get_user_count=AsyncMock(return_value=3),
            get_generation_preferences=AsyncMock(return_value={}),
            update_generation_preferences=AsyncMock(return_value=True),
            change_access=AsyncMock(),
        )
        self.presets = {
            kind: [CommonParam(name=name, value=value, type=kind)]
            for kind, name, value in (
                (ParamType.IMAGE_SIZE, 'Средний', 64),
                (ParamType.NOISE_LEVEL, 'Средний', 30),
                (ParamType.INSET_SIZE, 'Средний', 40),
            )
        }
        library = ImageGroup(imager_name='icons', display_name='Иконки', count=2, last_synced=now)
        self.parameters = SimpleNamespace(
            get_common_params=AsyncMock(side_effect=lambda kind: Result.Success(self.presets[kind])),
            add_common_param=AsyncMock(),
        )
        self.groups = SimpleNamespace(
            get_groups=AsyncMock(return_value=Result.Success([library])),
            get_group_by_name=AsyncMock(return_value=library),
            update_group_status=AsyncMock(),
        )
        repositories = SimpleNamespace(
            user_repository=self.users, image_build_params_repository=self.parameters,
            image_group_repository=self.groups, admission_repository=SimpleNamespace(),
        )
        self.dispatcher = importlib.import_module('telegram_bot.main').create_dispatcher(repositories, SimpleNamespace())
        self.sequence = 0
        self.clock = time.monotonic()
        # Advance an injected monotonic clock; production limiter stays installed.
        for middleware in self.dispatcher.update.outer_middleware:
            if isinstance(middleware, AbuseControlMiddleware):
                middleware.limiter.clock = lambda: self.clock

    async def asyncTearDown(self):
        await self.dispatcher.fsm.close()
        await self.bot.session.close()
        self.assertEqual(self.session.unsupported, [])
        for method in self.session.methods:
            self.assertNotIn('Не удалось выполнить действие', getattr(method, 'text', None) or '')

    def actor(self, actor_id):
        return TelegramUser(id=actor_id, is_bot=False, first_name='Тест')

    async def send(self, actor_id, text):
        self.sequence += 1
        self.clock += 2
        await self.dispatcher.feed_update(self.bot, Update(
            update_id=self.sequence,
            message=Message(
                message_id=self.sequence, date=datetime.now(timezone.utc),
                chat=Chat(id=actor_id, type='private'), from_user=self.actor(actor_id), text=text,
            ),
        ))
        return self.session.last.get(actor_id)

    async def click(self, actor_id, *, title=None, message=None, payload=None):
        message = message or self.session.last[actor_id]
        if payload is None:
            payload = next(button.callback_data for row in message.reply_markup.inline_keyboard
                           for button in row if title in button.text)
        self.sequence += 1
        self.clock += 2
        await self.dispatcher.feed_update(self.bot, Update(
            update_id=self.sequence, callback_query=CallbackQuery(
                id=f'callback-{self.sequence}', from_user=self.actor(actor_id), chat_instance='private',
                message=message, data=payload,
            ),
        ))
        return self.session.last.get(actor_id)

    def state(self, actor_id):
        return self.dispatcher.fsm.get_context(bot=self.bot, chat_id=actor_id, user_id=actor_id)

    async def test_admin_command_reaches_fixed_and_database_admin(self):
        for actor_id in (999, 2):
            with self.subTest(actor_id=actor_id):
                response = await self.send(actor_id, '/admin')
                self.assertEqual(response.text, 'Админ-панель')
                self.assertTrue(response.reply_markup.inline_keyboard[0][0].callback_data.startswith('adm:'))

    async def test_import_command_takes_precedence_over_an_admin_value_prompt(self):
        from src.commands.admin_panel.pack_import.handlers import PackImportStates
        for previous in (CommonParamsStates.waiting_for_name, CommonParamsStates.waiting_for_value):
            with self.subTest(previous=previous.state):
                await self.state(999).set_state(previous)
                response = await self.send(999, '/import_pack')
                self.assertIn('ZIP', response.text)
                self.assertEqual(await self.state(999).get_state(), PackImportStates.waiting_for_archive.state)

    async def test_admin_command_is_not_consumed_as_a_pack_name(self):
        from src.commands.admin_panel.pack_import.handlers import PackImportStates
        await self.state(999).set_state(PackImportStates.waiting_for_name)
        response = await self.send(999, '/admin')
        self.assertEqual(response.text, 'Админ-панель')
        self.assertIsNone(await self.state(999).get_state())

    async def test_ordinary_user_cannot_use_nested_admin_callback(self):
        admin_menu = await self.send(999, '/admin')
        await self.click(3, title='Пользователи', message=admin_menu)
        self.users.get_user_count.assert_not_awaited()
        self.users.change_access.assert_not_awaited()
        answers = [method for method in self.session.methods if isinstance(method, AnswerCallbackQuery)]
        self.assertTrue(answers)
        self.assertIsNone(await self.state(3).get_state())

    async def test_preset_generation_flow_reaches_photo_upload_with_resolved_values(self):
        await self.send(3, '/menu')
        await self.click(3, title='Собрать изображение')
        await self.click(3, title='Качество (реком.)')
        await self.click(3, title='Средний')
        self.assertEqual(await self.state(3).get_state(), GenerationStates.selecting_image_group.state)
        # Back from libraries returns to whole profiles, not a retired numeric
        # parameter step. Selecting a profile again resolves all three values.
        await self.click(3, title='Назад')
        self.assertEqual(await self.state(3).get_state(), GenerationStates.selecting_image_size.state)
        self.assertIn('Выберите качество', self.session.last[3].text)
        await self.click(3, title='Средний')
        await self.click(3, title='Иконки')
        self.assertEqual(await self.state(3).get_state(), GenerationStates.waiting_for_image_upload.state)
        data = await self.state(3).get_data()
        self.assertEqual((data['image_size'], data['noise_level'], data['inset_size'], data['image_group']),
                         (64, 30, 40, 'icons'))
        self.assertEqual(set(data['selected_presets']), {'image_size', 'noise_level', 'inset_size'})
        self.assertIn('Отправьте фото или стикер', self.session.last[3].text)
        self.assertIn('Для анимированных и видеостикеров используется превью', self.session.last[3].text)
        await self.click(3, title='Назад')
        self.assertEqual(await self.state(3).get_state(), GenerationStates.selecting_image_group.state)
        await self.click(3, title='Иконки')

        self.sequence += 1
        self.clock += 2
        photo = Message(
            message_id=self.sequence, date=datetime.now(timezone.utc),
            chat=Chat(id=3, type='private'), from_user=self.actor(3),
            photo=[{'file_id': 'offline-photo', 'file_unique_id': 'photo-identity',
                    'width': 256, 'height': 256}],
        ).as_(self.bot)
        job_id = generation_job_id(photo)
        accepted = AsyncMock(return_value=SimpleNamespace(job_id=job_id, duplicate=False))
        progress = AsyncMock()
        self.dispatcher['generation_service'] = SimpleNamespace(execute=accepted, gateway=SimpleNamespace())
        self.dispatcher['generation_progress'] = SimpleNamespace(watch=progress)
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.sequence, message=photo))
        submitted = accepted.await_args.kwargs
        self.assertEqual(submitted['actor_id'], 3)
        self.assertEqual(submitted['job_id'], job_id)
        draft = submitted['draft']
        self.assertEqual((draft['image_size'], draft['noise_level'], draft['inset_size'], draft['image_group']),
                         (64, 30, 40, 'icons'))
        progress.assert_awaited_once()
        self.assertEqual(progress.await_args.kwargs, {'job_id': job_id, 'actor_id': 3, 'duplicate': False})

    async def test_menu_and_cancel_clear_user_and_admin_input_states(self):
        for command in ('/menu', '/cancel'):
            for actor_id, fsm in ((3, GenerationStates.waiting_manual_noise_level),
                                  (999, CommonParamsStates.waiting_for_value)):
                with self.subTest(command=command, actor=actor_id):
                    await self.state(actor_id).set_state(fsm)
                    await self.state(actor_id).set_data({'draft_id': 'old', 'admin_context': {'param_name': 'old'}})
                    await self.send(actor_id, command)
                    self.assertIsNone(await self.state(actor_id).get_state())
                    data = await self.state(actor_id).get_data()
                    self.assertNotIn('draft_id', data)
                    self.assertNotIn('admin_context', data)
        self.parameters.add_common_param.assert_not_awaited()

    async def test_previous_keyboard_is_rejected_after_new_menu_without_business_writes(self):
        old_menu = await self.send(3, '/menu')
        await self.send(3, '/menu')
        current = await self.state(3).get_data()
        await self.click(3, title='Собрать изображение', message=old_menu)
        self.assertEqual(await self.state(3).get_data(), current)
        self.assertIsNone(await self.state(3).get_state())
        self.users.change_access.assert_not_awaited()
        self.parameters.add_common_param.assert_not_awaited()
        self.groups.update_group_status.assert_not_awaited()
        answer = [method for method in self.session.methods if isinstance(method, AnswerCallbackQuery)][-1]
        self.assertIn('устарел', answer.text)
