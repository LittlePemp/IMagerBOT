"""Support sends stay bot-scoped and survive duplicate updates and send uncertainty."""
import asyncio
import copy
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import CopyMessage, ForwardMessage, SendMessage
from aiogram.types import Chat, Message, MessageId, PhotoSize, Update, User as TelegramUser

from src.support.handlers import create_support_dispatcher
from src.support.service import SupportService
from src.support.repository import SupportQueueFull


def person(identifier, *, admin=False, banned=False):
    return SimpleNamespace(telegram_id=identifier, is_admin=admin, isbanned=banned,
                           name='Анна', telegram_username='anna')


class MemorySupport:
    bot_id = 321

    def __init__(self):
        self.events, self.mappings, self.active = {}, {}, set()
        self.lock = asyncio.Lock()
        self.fail_ack = False

    async def accept_user_message(self, payload, recipient_ids):
        identifier = f"in:{payload['chat_id']}:{payload['message_id']}"
        if identifier in self.events:
            return False
        self.events[identifier] = {
            '_id': identifier, 'payload': copy.deepcopy(payload), 'user_chat_id': payload['chat_id'],
            'source_message_id': payload['message_id'], 'deliveries': {
                str(admin_id): {channel: {'state': 'pending', 'next_attempt_at': datetime.now(timezone.utc)}
                                for channel in ('main_alert', 'support_copy')} for admin_id in recipient_ids
            },
        }
        return True

    async def activate_admin(self, identifier):
        self.active.add(identifier)

    async def active_admin_ids(self):
        return list(self.active)

    async def next_delivery(self, channel, active_admin_ids=None):
        for identifier, event in self.events.items():
            for recipient, channels in event['deliveries'].items():
                item = channels.get(channel)
                if (item and item['state'] == 'pending'
                        and item['next_attempt_at'] <= datetime.now(timezone.utc)
                        and (active_admin_ids is None or int(recipient) in active_admin_ids)):
                    return {'event_id': identifier, 'channel': channel, 'recipient_id': int(recipient),
                            'event': copy.deepcopy(event)}
        return None

    def receipt(self, event_id, recipient, channel):
        return self.events[event_id]['deliveries'][str(recipient)][channel]

    async def reserve_delivery(self, event_id, recipient, channel):
        async with self.lock:
            item = self.receipt(event_id, recipient, channel)
            if item['state'] != 'pending':
                return False
            item['state'] = 'uncertain'
            return True

    async def finish_delivery(self, event_id, recipient, channel, *, state, message_id=None,
                              error=None, retry_at=None):
        if self.fail_ack and state == 'sent':
            self.fail_ack = False
            raise OSError('Mongo acknowledgement unavailable')
        item = self.receipt(event_id, recipient, channel)
        if item['state'] != 'uncertain':
            return False
        item.update(state=state, message_id=message_id, error=str(error) if error else None)
        if retry_at is not None:
            item['next_attempt_at'] = retry_at
        return True

    async def skip_delivery(self, event_id, recipient, channel):
        self.receipt(event_id, recipient, channel)['state'] = 'skipped'
        return True

    async def bind_admin_message(self, admin_id, admin_message_id, user_chat_id, source_message_id, event_id):
        self.mappings[(admin_id, admin_message_id)] = {
            'user_chat_id': user_chat_id, 'source_message_id': source_message_id, 'event_id': event_id,
        }

    async def get_reply_target(self, admin_id, reply_message_id):
        mapped = self.mappings.get((admin_id, reply_message_id))
        if mapped is not None:
            return copy.deepcopy(mapped)
        for event in self.events.values():
            receipt = event['deliveries'].get(str(admin_id), {}).get('support_copy')
            if receipt and receipt['state'] == 'sent' and receipt.get('message_id') == reply_message_id:
                return {'user_chat_id': event['user_chat_id'], 'source_message_id': event['source_message_id'],
                        'event_id': event['_id']}
        return None

    async def accept_admin_reply(self, payload, target):
        identifier = f"reply:{payload['chat_id']}:{payload['message_id']}"
        if identifier in self.events:
            return False
        self.events[identifier] = {
            '_id': identifier, 'payload': copy.deepcopy(payload), 'admin_id': payload['user_id'],
            'user_chat_id': target['user_chat_id'], 'source_message_id': payload['message_id'],
            'target': copy.deepcopy(target), 'deliveries': {str(target['user_chat_id']): {
                'outbound_reply': {'state': 'pending', 'next_attempt_at': datetime.now(timezone.utc)},
            }},
        }
        return True

    async def find_main_alert(self, admin_id, message_id):
        for event in self.events.values():
            item = event['deliveries'].get(str(admin_id), {}).get('main_alert')
            if item and item.get('message_id') == message_id:
                return event
        return None


class SupportServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = MemorySupport()
        self.people = {7: person(7), 99: person(99, admin=True), 100: person(100, admin=True)}
        self.users = SimpleNamespace(
            get_user_by_id=AsyncMock(side_effect=lambda identifier: self.people.get(identifier)),
            get_admins=AsyncMock(return_value=[self.people[99]]), update_user_info=AsyncMock(),
        )
        self.main_bot = SimpleNamespace(id=123, send_message=AsyncMock(return_value=SimpleNamespace(message_id=51)))
        self.support_bot = SimpleNamespace(
            id=321, forward_message=AsyncMock(return_value=SimpleNamespace(message_id=61)),
            copy_message=AsyncMock(return_value=SimpleNamespace(message_id=71)),
        )
        self.service = self.consumer()

    def consumer(self, *, fixed=()):
        return SupportService(self.repository, self.users, self.main_bot, self.support_bot,
                              'https://t.me/SupportBot', fixed_admin_ids=fixed)

    def message(self, identifier=7, message_id=1, *, text='Помогите', photo=False, reply=None):
        return SimpleNamespace(
            chat=SimpleNamespace(id=identifier, type='private'), from_user=SimpleNamespace(id=identifier),
            message_id=message_id, content_type='photo' if photo else 'text',
            text=None if photo else text, caption='Скриншот' if photo else None,
            reply_to_message=SimpleNamespace(
                message_id=reply, from_user=SimpleNamespace(id=321, is_bot=True),
            ) if reply else None, answer=AsyncMock(),
        )

    async def accepted(self, *, photo=False):
        message = self.message(photo=photo)
        await self.service.accept_user_message(message, self.people[7])
        return message

    async def test_admin_sees_main_alert_before_starting_support_and_media_waits(self):
        message = await self.accepted(photo=True)
        await self.service.deliver_once()
        notice = self.main_bot.send_message.await_args.kwargs
        self.assertIn('Анна (@anna)', notice['text'])
        self.assertIn('Скриншот', notice['text'])
        self.assertIsNone(notice['parse_mode'])
        self.assertTrue(notice['link_preview_options'].is_disabled)
        self.assertEqual(notice['reply_markup'].inline_keyboard[0][0].url, 'https://t.me/SupportBot?start=staff')
        self.support_bot.forward_message.assert_not_awaited()
        await self.repository.activate_admin(99)
        await self.service.deliver_once()
        self.support_bot.forward_message.assert_awaited_once_with(chat_id=99, from_chat_id=7, message_id=1)
        self.assertEqual(self.repository.mappings[(99, 61)]['user_chat_id'], 7)
        self.support_bot.copy_message.assert_not_awaited()
        message.answer.assert_awaited_once()

    async def test_reply_is_sent_by_support_bot_and_mapping_is_bound_to_the_admin(self):
        await self.accepted()
        await self.repository.activate_admin(99)
        await self.service.deliver_once()
        self.assertFalse(await self.service.accept_admin_reply(self.message(100, 2, reply=61), self.people[100]))
        reply = self.message(99, 3, reply=61, text='Уже исправлено')
        self.assertTrue(await self.service.accept_admin_reply(reply, self.people[99]))
        self.assertFalse(await self.service.accept_admin_reply(reply, self.people[99]))
        for _ in range(3):
            await self.service.deliver_once()
        sent = self.support_bot.copy_message.await_args.kwargs
        self.assertEqual((sent['chat_id'], sent['from_chat_id'], sent['message_id']), (7, 99, 3))
        self.assertEqual(sent['reply_parameters'].message_id, 1)
        self.support_bot.copy_message.assert_awaited_once()

    async def test_duplicate_incoming_update_is_not_acknowledged_or_notified_twice(self):
        message = await self.accepted()
        self.assertFalse(await self.service.accept_user_message(message, self.people[7]))
        message.answer.assert_awaited_once()
        await asyncio.gather(self.consumer().deliver_once(), self.consumer().deliver_once())
        self.main_bot.send_message.assert_awaited_once()

    async def test_unknown_send_outcome_survives_restart_without_repeating_that_channel(self):
        await self.accepted()
        self.main_bot.send_message.side_effect = TimeoutError('Unknown response')
        await self.service.deliver_once()
        self.assertEqual(self.repository.receipt('in:7:1', 99, 'main_alert')['state'], 'uncertain')
        await self.consumer().deliver_once()
        self.main_bot.send_message.assert_awaited_once()

    async def test_known_forward_success_and_lost_ack_keep_reply_map_and_never_resend(self):
        await self.accepted()
        await self.repository.activate_admin(99)
        self.repository.fail_ack = True
        await self.service.deliver_once()
        self.assertIn((99, 61), self.repository.mappings)
        await self.consumer().deliver_once()
        self.support_bot.forward_message.assert_awaited_once()

    async def test_known_forward_receipt_recovers_a_reply_when_mapping_projection_failed(self):
        await self.accepted()
        await self.repository.activate_admin(99)
        self.repository.bind_admin_message = AsyncMock(side_effect=OSError('Mapping projection unavailable'))
        await self.service.deliver_once()
        self.assertEqual(self.repository.receipt('in:7:1', 99, 'support_copy')['state'], 'sent')
        self.assertEqual(self.repository.mappings, {})
        reply = self.message(99, 3, reply=61)
        self.assertTrue(await self.consumer().accept_admin_reply(reply, self.people[99]))
        for _ in range(3):
            await self.consumer().deliver_once()
        self.support_bot.forward_message.assert_awaited_once()
        self.support_bot.copy_message.assert_awaited_once()
        self.assertEqual(self.support_bot.copy_message.await_args.kwargs['chat_id'], 7)

    async def test_explicit_floodwait_only_retries_after_its_due_time(self):
        await self.accepted()
        self.main_bot.send_message.side_effect = TelegramRetryAfter(
            method=SendMessage(chat_id=99, text='test'), message='Too many requests', retry_after=1,
        )
        await self.service.deliver_once()
        receipt = self.repository.receipt('in:7:1', 99, 'main_alert')
        self.assertEqual(receipt['state'], 'pending')
        await self.consumer().deliver_once()
        self.main_bot.send_message.assert_awaited_once()
        receipt['next_attempt_at'] = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.main_bot.send_message.side_effect = None
        await self.consumer().deliver_once()
        self.assertEqual(self.main_bot.send_message.await_count, 2)
        self.assertEqual(receipt['state'], 'sent')

    async def test_revoked_or_banned_admin_cannot_receive_or_answer_existing_support(self):
        await self.accepted()
        self.people[99].is_admin = False
        await self.service.deliver_once()
        self.main_bot.send_message.assert_not_awaited()
        self.assertEqual(self.repository.receipt('in:7:1', 99, 'main_alert')['state'], 'skipped')
        self.assertFalse(await self.service.accept_admin_reply(self.message(99, 2, reply=61), self.people[99]))

    async def test_staff_role_is_checked_again_before_a_queued_reply_is_sent(self):
        await self.repository.bind_admin_message(99, 61, 7, 1, 'original')
        await self.service.accept_admin_reply(self.message(99, 3, reply=61), self.people[99])
        self.people[99].isbanned = True
        await self.service.deliver_once()
        self.support_bot.copy_message.assert_not_awaited()

    async def test_staff_capacity_failure_is_friendly_and_keeps_the_original_reply_binding(self):
        await self.repository.bind_admin_message(99, 61, 7, 1, 'original')
        self.repository.accept_admin_reply = AsyncMock(side_effect=SupportQueueFull('Поддержка временно занята.'))
        message = self.message(99, 3, reply=61)
        self.assertFalse(await self.service.accept_admin_reply(message, self.people[99]))
        self.assertEqual(message.answer.await_args.args[0], 'Поддержка временно занята.')
        self.assertEqual((await self.repository.get_reply_target(99, 61))['user_chat_id'], 7)
        self.support_bot.copy_message.assert_not_awaited()

    async def test_fixed_admins_are_deduplicated_and_keep_access_even_if_database_role_is_changed(self):
        self.people[99].isbanned = True
        service = self.consumer(fixed=(99, 100))
        self.assertTrue(await service.is_admin(99))
        await service.accept_user_message(self.message(), self.people[7])
        self.assertEqual(set(self.repository.events['in:7:1']['deliveries']), {'99', '100'})

    async def test_untrusted_text_has_a_bounded_plain_text_alert_without_broken_emoji(self):
        message = self.message(text='<b>raw</b>\n' + '🙂' * 4000)
        await self.service.accept_user_message(message, self.people[7])
        await self.service.deliver_once()
        text = self.main_bot.send_message.await_args.kwargs['text']
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, self.service.policy.max_text_length)
        self.assertIn('<b>raw</b>', text)
        self.assertNotIn('\ufffd', text)

    async def test_support_start_activates_only_staff_and_customer_receives_instructions(self):
        customer, staff = self.message(), self.message(99)
        await self.service.start(customer, self.people[7])
        self.assertEqual(self.repository.active, set())
        await self.service.start(staff, self.people[99])
        self.assertEqual(self.repository.active, {99})
        self.assertIn('Опишите проблему', customer.answer.await_args.args[0])
        self.assertIn('пересланное', staff.answer.await_args.args[0])

    async def test_unsupported_or_foreign_chat_cannot_create_a_support_request(self):
        message = self.message()
        message.content_type = 'contact'
        self.assertFalse(await self.service.accept_user_message(message, self.people[7]))
        self.users.get_admins.assert_not_awaited()
        message.chat.type = 'group'
        with self.assertRaises(ValueError):
            await self.service.accept_user_message(message, self.people[7])
        self.assertEqual(self.repository.events, {})

    async def test_nonprivate_or_wrong_actor_cannot_activate_staff_or_resolve_private_reply_bindings(self):
        self.repository.get_reply_target = AsyncMock()
        self.users.get_user_by_id.reset_mock()
        for chat_type, chat_id, sender_id in (('group', 99, 99), ('private', 99, 100)):
            with self.subTest(chat_type=chat_type, sender=sender_id):
                message = self.message(99, reply=61)
                message.chat.type, message.chat.id, message.from_user.id = chat_type, chat_id, sender_id
                with self.assertRaises(ValueError):
                    await self.service.start(message, self.people[99])
                with self.assertRaises(ValueError):
                    await self.service.accept_admin_reply(message, self.people[99])
        self.assertEqual(self.repository.active, set())
        self.users.get_user_by_id.assert_not_awaited()
        self.repository.get_reply_target.assert_not_awaited()

    async def test_reply_to_another_bots_message_never_resolves_a_support_binding(self):
        await self.repository.bind_admin_message(99, 61, 7, 1, 'original')
        self.repository.get_reply_target = AsyncMock()
        message = self.message(99, reply=61)
        message.reply_to_message.from_user.id = 123
        self.assertFalse(await self.service.accept_admin_reply(message, self.people[99]))
        self.repository.get_reply_target.assert_not_awaited()


class SupportSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.methods = []
        self.last = {}
        self.sequence = 100

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise AssertionError('Support must not download any files')
        yield b''

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        self.sequence += 1
        if isinstance(method, CopyMessage):
            return MessageId(message_id=self.sequence)
        if isinstance(method, (SendMessage, ForwardMessage)):
            message = Message(
                message_id=self.sequence, date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type='private'),
                from_user=TelegramUser(id=bot.id, is_bot=True, first_name='Support'),
                text=method.text if isinstance(method, SendMessage) else 'User original',
            ).as_(bot)
            self.last[method.chat_id] = message
            return message
        raise AssertionError(f'Unexpected support API: {type(method).__name__}')


class SupportDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session, self.main_session = SupportSession(), SupportSession()
        self.bot = Bot(token='321:OFFLINE_ONLY_TOKEN_000000000000000000', session=self.session)
        self.main_bot = Bot(token='123:OFFLINE_ONLY_TOKEN_000000000000000000', session=self.main_session)
        self.people = {7: person(7), 99: person(99, admin=True)}
        self.users = SimpleNamespace(
            get_user_by_id=AsyncMock(side_effect=lambda identifier: self.people.get(identifier)),
            get_admins=AsyncMock(return_value=[self.people[99]]), update_user_info=AsyncMock(),
        )
        self.repository = MemorySupport()
        self.service = SupportService(self.repository, self.users, self.main_bot, self.bot, 'https://t.me/SupportBot')
        self.dispatcher = create_support_dispatcher(self.users, self.service)
        self.sequence = 0
        self.clock = time.monotonic()
        for middleware in self.dispatcher.update.outer_middleware:
            if hasattr(middleware, 'limiter'):
                middleware.limiter.clock = lambda: self.clock

    async def asyncTearDown(self):
        await self.dispatcher.fsm.close()
        await self.bot.session.close()
        await self.main_bot.session.close()

    async def send(self, identifier, text=None, *, photo=False, reply=None, advance=20):
        self.sequence += 1
        self.clock += advance
        message = Message(
            message_id=self.sequence, date=datetime.now(timezone.utc),
            chat=Chat(id=identifier, type='private'),
            from_user=TelegramUser(id=identifier, first_name='User', is_bot=False),
            text=None if photo else text,
            photo=[PhotoSize(file_id='photo', file_unique_id='photo', width=512, height=512)] if photo else None,
            reply_to_message=reply,
        ).as_(self.bot)
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.sequence, message=message))
        return message

    async def test_photo_enters_only_support_and_admin_reply_routes_back_to_its_sender(self):
        await self.send(7, '/start')
        incoming = await self.send(7, photo=True)
        self.assertEqual(len(self.repository.events), 1)
        await self.service.deliver_once()
        self.assertIsInstance(self.main_session.methods[-1], SendMessage)
        await self.send(99, '/start staff')
        await self.service.deliver_once()
        forwarded = self.session.last[99]
        reply = await self.send(99, 'Попробуйте ещё раз', reply=forwarded)
        await self.service.deliver_once()
        outgoing = self.session.methods[-1]
        self.assertIsInstance(outgoing, CopyMessage)
        self.assertEqual((outgoing.chat_id, outgoing.from_chat_id, outgoing.message_id), (7, 99, reply.message_id))
        self.assertEqual(outgoing.reply_parameters.message_id, incoming.message_id)

    async def test_non_admin_reply_does_not_gain_staff_routing_and_private_gate_rejects_groups(self):
        await self.repository.bind_admin_message(99, 61, 100, 1, 'private')
        fake_reference = Message(
            message_id=61, date=datetime.now(timezone.utc), chat=Chat(id=7, type='private'),
            from_user=TelegramUser(id=self.bot.id, first_name='Support', is_bot=True), text='old',
        )
        await self.send(7, 'forged', reply=fake_reference)
        self.assertTrue(all(not key.startswith('reply:') for key in self.repository.events))
        self.assertEqual(next(iter(self.repository.events.values()))['user_chat_id'], 7)

    async def test_main_support_alert_text_and_photo_replies_are_redirected_before_form_recovery_or_generation(self):
        from telegram_bot.main import create_dispatcher

        repositories = SimpleNamespace(user_repository=self.users, admission_repository=SimpleNamespace(),
                                       image_group_repository=SimpleNamespace(), image_build_params_repository=SimpleNamespace())
        main = create_dispatcher(repositories, SimpleNamespace(), support_service=self.service)
        accepted = AsyncMock(return_value=SimpleNamespace(job_id='accepted', duplicate=False))
        main['generation_service'] = SimpleNamespace(execute=accepted, gateway=object())
        main['generation_progress'] = SimpleNamespace(watch=AsyncMock())
        for middleware in main.update.outer_middleware:
            if hasattr(middleware, 'limiter'):
                middleware.limiter.clock = lambda: self.clock
        await self.repository.accept_user_message({
            'chat_id': 7, 'message_id': 1, 'user_id': 7, 'text': 'Help', 'content_type': 'text',
        }, [99])
        self.repository.receipt('in:7:1', 99, 'main_alert').update(state='sent', message_id=51)
        original = Message(
            message_id=51, date=datetime.now(timezone.utc), chat=Chat(id=99, type='private'),
            from_user=TelegramUser(id=self.main_bot.id, first_name='Main', is_bot=True), text='Support notice',
        )
        try:
            for photo in (False, True):
                with self.subTest(photo=photo):
                    self.sequence += 1
                    self.clock += 20
                    message = Message(
                        message_id=self.sequence, date=datetime.now(timezone.utc),
                        chat=Chat(id=99, type='private'), from_user=TelegramUser(id=99, first_name='Admin', is_bot=False),
                        text=None if photo else 'Reply', reply_to_message=original,
                        photo=[PhotoSize(file_id='photo', file_unique_id='photo', width=512, height=512)] if photo else None,
                    ).as_(self.main_bot)
                    await main.feed_update(self.main_bot, Update(update_id=self.sequence, message=message))
                    self.assertIn('Ответьте пользователю в боте поддержки', self.main_session.last[99].text)
                    state = main.fsm.get_context(bot=self.main_bot, chat_id=99, user_id=99)
                    self.assertEqual(await state.get_data(), {})
            accepted.assert_not_awaited()
        finally:
            await main.fsm.close()

    async def test_unrelated_main_photo_reply_keeps_its_normal_generation_route(self):
        from telegram_bot.main import create_dispatcher

        repositories = SimpleNamespace(user_repository=self.users, admission_repository=SimpleNamespace(),
                                       image_group_repository=SimpleNamespace(), image_build_params_repository=SimpleNamespace())
        main = create_dispatcher(repositories, SimpleNamespace(), support_service=self.service)
        accepted = AsyncMock(return_value=SimpleNamespace(job_id='accepted', duplicate=False))
        main['generation_service'] = SimpleNamespace(execute=accepted, gateway=object())
        main['generation_progress'] = SimpleNamespace(watch=AsyncMock())
        original = Message(
            message_id=52, date=datetime.now(timezone.utc), chat=Chat(id=99, type='private'),
            from_user=TelegramUser(id=self.main_bot.id, first_name='Main', is_bot=True), text='Different menu',
        )
        message = Message(
            message_id=4, date=datetime.now(timezone.utc), chat=Chat(id=99, type='private'),
            from_user=TelegramUser(id=99, first_name='Admin', is_bot=False), reply_to_message=original,
            photo=[PhotoSize(file_id='photo', file_unique_id='photo', width=512, height=512)],
        ).as_(self.main_bot)
        try:
            await main.feed_update(self.main_bot, Update(update_id=4, message=message))
            accepted.assert_awaited_once()
        finally:
            await main.fsm.close()
