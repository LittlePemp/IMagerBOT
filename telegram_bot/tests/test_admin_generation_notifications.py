"""Admin delivery failures never repeat a possibly accepted Telegram message."""
import asyncio
import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from imager_config.loader import config
from src.application.admin_generation_notifications import (
    AdminGenerationNotifications, generation_notice_text,
)


def actor(identifier=7, *, admin=False, banned=False, name='Анна', username='anna'):
    return SimpleNamespace(telegram_id=identifier, is_admin=admin, isbanned=banned,
                           name=name, telegram_username=username)


class NoticeStore:
    """Persisted states survive consumer replacement; reservation is atomic."""
    def __init__(self, recipients=None):
        self.lock = asyncio.Lock()
        now = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.notice = {
            'job_id': 'e904cedf-43a8-543c-914c-4579586f628c', 'actor_id': 7,
            'accepted_at': now,
            'document': {'request': {
                'parameters': {'group_name': 'emoji', 'result_size': 128,
                               'cell_size': 40, 'noise_level': 30},
                'source': {'key': 'private/source.jpg', 'sha256': 'private-checksum'},
                'client_context': {'chat_id': 7, 'history_record_id': 'private-history'},
            }},
            'admin_notice': {'state': 'pending', 'next_attempt_at': now, 'recipients': None},
        }
        if recipients is not None:
            self.notice['admin_notice']['recipients'] = {
                str(identifier): {'state': 'pending', 'next_attempt_at': now} for identifier in recipients
            }
        self.ack_failure_for = None
        self.initializations = []

    async def next_admin_notice(self):
        async with self.lock:
            item = self.notice['admin_notice']
            if item['state'] != 'pending' or item['next_attempt_at'] > datetime.now(timezone.utc):
                return None
            return copy.deepcopy(self.notice)

    async def initialize_admin_notice(self, job_id, recipients):
        async with self.lock:
            item = self.notice['admin_notice']
            if item['recipients'] is not None:
                return False
            self.initializations.append(list(recipients))
            now = datetime.now(timezone.utc)
            item['recipients'] = {str(identifier): {'state': 'pending', 'next_attempt_at': now}
                                  for identifier in recipients}
            item['next_attempt_at'] = now
            return True

    async def claim_admin_notice_recipient(self, job_id, admin_id):
        async with self.lock:
            item = self.receipt(admin_id)
            if item['state'] != 'pending' or item['next_attempt_at'] > datetime.now(timezone.utc):
                return False
            item['state'] = 'uncertain'
            return True

    async def update_admin_notice_recipient(self, job_id, admin_id, *, state, message_id=None,
                                            error=None, retry_at=None):
        async with self.lock:
            if self.ack_failure_for == admin_id and state == 'sent':
                self.ack_failure_for = None
                raise OSError('Mongo acknowledgement unavailable after Telegram success')
            item = self.receipt(admin_id)
            if item['state'] != 'uncertain':
                return False
            item.update(state=state, message_id=message_id, error=str(error) if error is not None else None)
            if retry_at is not None:
                item['next_attempt_at'] = retry_at
                self.notice['admin_notice']['state'] = 'pending'
            return True

    async def skip_admin_notice_recipient(self, job_id, admin_id):
        async with self.lock:
            item = self.receipt(admin_id)
            if item['state'] != 'pending':
                return False
            item['state'] = 'skipped'
            return True

    async def complete_admin_notice(self, job_id):
        async with self.lock:
            notice = self.notice['admin_notice']
            if notice['recipients'] is None:
                return False
            pending = [item['next_attempt_at'] for item in notice['recipients'].values()
                       if item['state'] == 'pending']
            if pending:
                notice['next_attempt_at'] = min(pending)
                return False
            notice['state'] = 'done'
            return True

    def receipt(self, admin_id):
        return self.notice['admin_notice']['recipients'][str(admin_id)]


class AdminNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = NoticeStore([99])
        self.people = {7: actor(), 99: actor(99, admin=True), 100: actor(100, admin=True)}
        self.users = SimpleNamespace(
            get_admins=AsyncMock(return_value=[self.people[99]]),
            get_user_by_id=AsyncMock(side_effect=lambda identifier: self.people.get(identifier)),
        )
        self.groups = SimpleNamespace(get_group_by_name=AsyncMock(
            return_value=SimpleNamespace(display_name='Emoji'),
        ))
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=51)))

    def consumer(self, *, fixed=()):
        return AdminGenerationNotifications(self.store, self.users, self.groups, self.bot,
                                            fixed_admin_ids=fixed)

    async def test_new_recipient_snapshot_unions_fixed_and_current_roles_without_duplicates(self):
        self.store = NoticeStore()
        self.users.get_admins.return_value = [
            self.people[99], self.people[100], actor(101, admin=True, banned=True), actor(102),
        ]
        consumer = self.consumer(fixed=(99, 103))
        self.assertTrue(await consumer.deliver_once())
        self.assertEqual(self.store.initializations, [[99, 100, 103]])
        self.bot.send_message.assert_not_awaited()
        self.assertTrue(await consumer.deliver_once())
        self.assertEqual([call.kwargs['chat_id'] for call in self.bot.send_message.await_args_list], [99, 100, 103])
        self.assertFalse(await self.consumer(fixed=(99, 103)).deliver_once())

    async def test_notice_uses_frozen_parameters_and_plain_text_without_images_or_internal_references(self):
        async def send(**kwargs):
            self.assertEqual(self.store.receipt(kwargs['chat_id'])['state'], 'uncertain')
            return SimpleNamespace(message_id=51)
        self.bot.send_message.side_effect = send
        await self.consumer().deliver_once()
        values = self.bot.send_message.await_args.kwargs
        self.assertEqual(values['chat_id'], 99)
        self.assertIsNone(values['parse_mode'])
        self.assertTrue(values['link_preview_options'].is_disabled)
        text = values['text']
        for expected in ('принято в очередь', 'Анна (@anna)', 'Telegram ID: 7', 'Emoji', '128', '40', '30'):
            self.assertIn(expected, text)
        for private in (self.store.notice['job_id'], 'private/source.jpg', 'private-checksum', 'private-history'):
            self.assertNotIn(private, text)
        self.assertEqual(self.store.receipt(99)['state'], 'sent')
        self.assertEqual(self.store.receipt(99)['message_id'], 51)

    async def test_concurrent_consumers_reserve_one_attempt_for_a_recipient(self):
        await asyncio.gather(self.consumer().deliver_once(), self.consumer().deliver_once())
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.store.receipt(99)['state'], 'sent')

    async def test_unknown_send_outcome_is_terminal_and_does_not_block_next_admin(self):
        self.store = NoticeStore([99, 100])
        self.bot.send_message.side_effect = [TimeoutError('Unknown response'), SimpleNamespace(message_id=52)]
        await self.consumer().deliver_once()
        self.assertEqual(self.store.receipt(99)['state'], 'uncertain')
        self.assertEqual(self.store.receipt(100)['state'], 'sent')
        self.assertFalse(await self.consumer().deliver_once())
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_cancellation_after_reservation_keeps_next_recipient_recoverable(self):
        self.store = NoticeStore([99, 100])
        self.bot.send_message.side_effect = [asyncio.CancelledError(), SimpleNamespace(message_id=52)]
        with self.assertRaises(asyncio.CancelledError):
            await self.consumer().deliver_once()
        self.assertEqual(self.store.receipt(99)['state'], 'uncertain')
        self.assertEqual(self.store.receipt(100)['state'], 'pending')
        await self.consumer().deliver_once()
        self.assertEqual([call.kwargs['chat_id'] for call in self.bot.send_message.await_args_list], [99, 100])

    async def test_lost_mongo_ack_after_known_telegram_success_never_repeats_that_admin(self):
        self.store = NoticeStore([99, 100])
        self.store.ack_failure_for = 99
        with self.assertRaises(OSError):
            await self.consumer().deliver_once()
        self.assertEqual(self.store.receipt(99)['state'], 'uncertain')
        await self.consumer().deliver_once()
        self.assertEqual([call.kwargs['chat_id'] for call in self.bot.send_message.await_args_list], [99, 100])

    async def test_demoted_or_banned_recipient_is_skipped_but_permanent_admin_remains_eligible(self):
        for banned in (False, True):
            with self.subTest(banned=banned):
                self.store = NoticeStore([99, 100])
                self.people[99] = actor(99, admin=banned, banned=banned)
                self.people[100] = actor(100, admin=False, banned=True)
                self.bot.send_message.reset_mock()
                await self.consumer(fixed=(100,)).deliver_once()
                self.assertEqual(self.store.receipt(99)['state'], 'skipped')
                self.bot.send_message.assert_awaited_once()
                self.assertEqual(self.bot.send_message.await_args.kwargs['chat_id'], 100)

    async def test_admin_generating_own_image_also_receives_notice(self):
        self.store = NoticeStore([7])
        self.people[7].is_admin = True
        await self.consumer().deliver_once()
        self.assertEqual(self.bot.send_message.await_args.kwargs['chat_id'], 7)

    async def test_definite_rejections_are_terminal_and_other_admins_still_receive(self):
        for exception in (TelegramBadRequest, TelegramForbiddenError):
            with self.subTest(exception=exception):
                self.store = NoticeStore([99, 100])
                self.bot.send_message.reset_mock()
                self.bot.send_message.side_effect = [
                    exception(method=SendMessage(chat_id=99, text='notice'), message='Rejected'),
                    SimpleNamespace(message_id=52),
                ]
                await self.consumer().deliver_once()
                self.assertEqual(self.store.receipt(99)['state'], 'failed')
                self.assertEqual(self.store.receipt(100)['state'], 'sent')
                self.assertFalse(await self.consumer().deliver_once())
                self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_only_explicit_floodwait_can_retry_and_future_recipient_does_not_block_sibling(self):
        self.store = NoticeStore([99, 100])
        self.bot.send_message.side_effect = [
            TelegramRetryAfter(method=SendMessage(chat_id=99, text='notice'), message='Wait', retry_after=30),
            SimpleNamespace(message_id=52), SimpleNamespace(message_id=53),
        ]
        consumer = self.consumer()
        before = datetime.now(timezone.utc)
        await consumer.deliver_once()
        self.assertEqual(self.store.receipt(99)['state'], 'pending')
        self.assertGreaterEqual(self.store.receipt(99)['next_attempt_at'],
                                before + timedelta(seconds=30 + config.bot.notification_retry_after_grace_seconds))
        self.assertFalse(await consumer.deliver_once())
        self.assertEqual(self.bot.send_message.await_count, 1)
        # A replacement consumer honors persisted per-recipient time even though
        # the process-local bot-wide pause is gone. Recipient 100 is still due.
        await self.consumer().deliver_once()
        self.assertEqual(self.store.receipt(100)['state'], 'sent')
        self.assertEqual(self.store.receipt(99)['state'], 'pending')
        self.assertFalse(await self.consumer().deliver_once())
        self.store.receipt(99)['next_attempt_at'] = before - timedelta(seconds=1)
        self.store.notice['admin_notice']['next_attempt_at'] = before - timedelta(seconds=1)
        await self.consumer().deliver_once()
        self.assertEqual([call.kwargs['chat_id'] for call in self.bot.send_message.await_args_list], [99, 100, 99])
        self.assertEqual(self.store.receipt(99)['state'], 'sent')

    async def test_no_administrators_finishes_empty_fanout_without_telegram_io(self):
        self.store = NoticeStore()
        self.users.get_admins.return_value = []
        await self.consumer().deliver_once()
        self.assertEqual(self.store.notice['admin_notice']['state'], 'done')
        self.bot.send_message.assert_not_awaited()
        self.assertFalse(await self.consumer().deliver_once())

    async def test_recipient_lookup_failure_keeps_initialization_recoverable(self):
        self.store = NoticeStore()
        self.users.get_admins.side_effect = OSError('Mongo unavailable')
        with self.assertRaises(OSError):
            await self.consumer().deliver_once()
        self.assertIsNone(self.store.notice['admin_notice']['recipients'])
        self.assertEqual(self.store.notice['admin_notice']['state'], 'pending')
        self.bot.send_message.assert_not_awaited()


class AdminNotificationTextTests(unittest.TestCase):
    def test_missing_actor_or_removed_pack_still_gives_useful_notice(self):
        text = generation_notice_text(NoticeStore().notice, None, None)
        self.assertIn('Пользователь', text)
        self.assertIn('Telegram ID: 7', text)
        self.assertIn('Пак: emoji', text)

    def test_untrusted_labels_are_bounded_single_line_and_username_is_validated(self):
        user = actor(name='<b>Test</b>\x00\n' + 'x' * 10000, username='bad\nusername<script>')
        group = SimpleNamespace(display_name='Emoji\n' + 'y' * 10000)
        text = generation_notice_text(NoticeStore().notice, user, group)
        self.assertNotIn('bad', text)
        self.assertNotIn('\x00', text)
        self.assertIn('<b>Test</b>', text)  # Sent with parse_mode=None.
        self.assertEqual(len(text.splitlines()), 6)
        self.assertLess(len(text), config.bot.ui_name_max_length * 2 + 300)
