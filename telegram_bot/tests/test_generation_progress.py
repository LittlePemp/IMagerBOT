import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import EditMessageText

from src.application.generation_progress import (
    GenerationProgress, QUEUE_TEXT, BUILDING_TEXT, RENDERING_TEXT, DONE_TEXT,
    DELIVERY_FAILED_TEXT, DELIVERY_UNCERTAIN_TEXT, BUILD_FAILED_TEXT,
)


class Messages:
    def __init__(self):
        self.records = {}

    async def get(self, job_id):
        return copy.deepcopy(self.records.get(job_id))

    async def track(self, job_id, **values):
        self.records.setdefault(job_id, dict(
            _id=job_id, finished=False, last_text=values.pop('text'), **values,
        ))
        return await self.get(job_id)

    async def active(self, limit):
        return [copy.deepcopy(row) for row in self.records.values() if not row['finished']][:limit]

    async def remember(self, job_id, text, *, finished=False):
        self.records[job_id].update(last_text=text, finished=finished)
        return True


class GenerationProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime.now(timezone.utc)
        self.repository = Messages()
        self.admissions = SimpleNamespace(
            pending_submissions=AsyncMock(return_value=[]),
            find=AsyncMock(return_value=SimpleNamespace(state='pending')),
        )
        self.gateway = SimpleNamespace(
            get_job=AsyncMock(return_value=None), active_queue=AsyncMock(return_value=[]),
            progress_jobs=AsyncMock(return_value=[]),
        )
        self.bot = SimpleNamespace(edit_message_text=AsyncMock(), set_message_reaction=AsyncMock())
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=5), message_id=34,
            reply=AsyncMock(return_value=SimpleNamespace(message_id=40)),
        )
        self.progress = GenerationProgress(self.repository, self.admissions, self.gateway, self.bot)

    def job(self, job_id='image-1', status='queued', **values):
        return dict(
            _id=job_id, status=status, created_at=self.now,
            request={'client_context': {'user_id': 5, 'chat_id': 5}}, **values,
        )

    async def watch(self):
        self.admissions.pending_submissions.return_value = [self.job()]
        return await self.progress.watch(self.message, job_id='image-1', actor_id=5)

    async def test_queue_merges_admission_and_core_without_double_counting(self):
        older = self.job('older', 'running')
        older['created_at'] -= timedelta(seconds=1)
        self.gateway.active_queue.return_value = [older, self.job()]
        await self.watch()
        self.message.reply.assert_awaited_once_with(QUEUE_TEXT.format(position=2))
        reaction = self.bot.set_message_reaction.await_args.kwargs
        self.assertEqual((reaction['chat_id'], reaction['message_id']), (5, 34))
        self.assertEqual(reaction['reaction'][0].emoji, '👀')
        self.assertNotIn('reply_markup', self.message.reply.await_args.kwargs)

    async def test_completed_core_record_cannot_remain_a_pending_queue_ghost(self):
        completed = self.job('older', 'completed')
        completed['created_at'] -= timedelta(seconds=1)
        self.admissions.pending_submissions.return_value = [completed | {'status': 'queued'}, self.job()]
        self.gateway.active_queue.return_value = [completed, self.job()]
        queue = await self.progress._queue()
        self.assertEqual([job['_id'] for job in queue], ['image-1'])

    async def test_queue_changes_edit_original_message_and_position_one_is_not_running(self):
        await self.watch()
        older = self.job('older')
        older['created_at'] -= timedelta(seconds=1)
        self.gateway.active_queue.return_value = [older, self.job()]
        self.gateway.progress_jobs.return_value = [self.job()]
        await self.progress.poll_once()
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs, {
            'chat_id': 5, 'message_id': 40, 'text': QUEUE_TEXT.format(position=2),
        })
        self.gateway.active_queue.return_value = [self.job()]
        await self.progress.poll_once()
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs['text'], QUEUE_TEXT.format(position=1))
        self.message.reply.assert_awaited_once()

    async def test_real_worker_progress_updates_same_message_and_unchanged_text_is_skipped(self):
        await self.watch()
        running = self.job(status='running', execution={'progress': {
            'stage': 'rendering', 'completed_cells': 25, 'total_cells': 100,
        }})
        self.gateway.progress_jobs.return_value = [running]
        await self.progress.poll_once()
        await self.progress.poll_once()
        self.bot.edit_message_text.assert_awaited_once_with(
            chat_id=5, message_id=40, text=RENDERING_TEXT.format(percent=25),
        )

    async def test_backoff_does_not_show_previous_attempt_percentage(self):
        text, finished = self.progress._text(self.job(status='retrying', execution={'progress': {
            'stage': 'rendering', 'completed_cells': 80, 'total_cells': 100,
        }}), 1)
        self.assertEqual((text, finished), (BUILDING_TEXT, False))

    async def test_restart_and_duplicate_reuse_durable_target(self):
        await self.watch()
        restarted = GenerationProgress(self.repository, self.admissions, self.gateway, self.bot)
        await restarted.watch(self.message, job_id='image-1', actor_id=5, duplicate=True)
        self.gateway.progress_jobs.return_value = [self.job(status='running')]
        await restarted.poll_once()
        self.message.reply.assert_awaited_once()
        self.bot.edit_message_text.assert_awaited_once_with(chat_id=5, message_id=40, text=BUILDING_TEXT)

    async def test_publish_ack_between_snapshots_is_not_shown_as_rejected(self):
        await self.watch()
        self.admissions.pending_submissions.return_value = []
        self.admissions.find.return_value = SimpleNamespace(state='sent')
        self.gateway.get_job.return_value = self.job(status='completed', completion_outbox={'state': 'sent'})
        await self.progress.poll_once()
        self.bot.edit_message_text.assert_awaited_once_with(chat_id=5, message_id=40, text=DONE_TEXT)

    async def test_concurrent_successful_failure_edit_prevents_a_fallback_message(self):
        await self.watch()

        async def finish(*args, **kwargs):
            await self.repository.remember('image-1', BUILD_FAILED_TEXT, finished=True)
            return None

        self.progress._edit = AsyncMock(side_effect=finish)
        self.assertEqual(await self.progress.fail_generation(self.job(status='failed')), 40)

    async def test_terminal_outcomes_close_progress_without_delivery_controls(self):
        for state, expected in (
            ('sent', DONE_TEXT), ('failed', DELIVERY_FAILED_TEXT),
            ('uncertain', DELIVERY_UNCERTAIN_TEXT),
        ):
            with self.subTest(state=state):
                self.repository.records.clear()
                await self.watch()
                self.bot.edit_message_text.reset_mock()
                self.gateway.progress_jobs.return_value = [self.job(
                    status='completed', completion_outbox={'state': state},
                )]
                await self.progress.poll_once()
                await self.progress.poll_once()
                self.bot.edit_message_text.assert_awaited_once_with(chat_id=5, message_id=40, text=expected)
                self.assertTrue(self.repository.records['image-1']['finished'])

    async def test_failure_notification_edits_and_reuses_the_same_message(self):
        await self.watch()
        failed = self.job(status='failed')
        self.assertEqual(await self.progress.fail_generation(failed), 40)
        self.assertEqual(await self.progress.fail_generation(failed), 40)
        self.bot.edit_message_text.assert_awaited_once_with(chat_id=5, message_id=40, text=BUILD_FAILED_TEXT)

    async def test_disabled_reaction_does_not_lose_accepted_progress(self):
        self.bot.set_message_reaction.side_effect = RuntimeError('reactions disabled')
        record = await self.watch()
        self.assertEqual(record['message_id'], 40)
        self.assertIn('image-1', self.repository.records)

    async def test_wrong_owner_is_never_shown(self):
        await self.watch()
        wrong = self.job(status='running')
        wrong['request']['client_context']['user_id'] = 9
        self.gateway.progress_jobs.return_value = [wrong]
        await self.progress.poll_once()
        self.bot.edit_message_text.assert_not_awaited()
        self.assertTrue(self.repository.records['image-1']['finished'])

    async def test_deleted_status_message_does_not_restart_delivery_or_loop_forever(self):
        await self.watch()
        self.bot.edit_message_text.side_effect = TelegramBadRequest(
            method=EditMessageText(text='test', chat_id=5, message_id=40),
            message='message to edit not found',
        )
        self.assertIsNone(await self.progress.fail_generation(self.job(status='failed')))
        self.assertIsNone(await self.progress.fail_generation(self.job(status='failed')))
        self.assertTrue(self.repository.records['image-1']['finished'])

    async def test_uncertain_failure_edit_must_not_fall_back_to_another_message(self):
        from src.application.generation_progress import ProgressUpdateUncertain
        await self.watch()
        self.bot.edit_message_text.side_effect = TimeoutError('lost response')
        with self.assertRaises(ProgressUpdateUncertain):
            await self.progress.fail_generation(self.job(status='failed'))
        self.message.reply.assert_awaited_once()
        self.assertFalse(self.repository.records['image-1']['finished'])

    async def test_floodwait_pauses_progress_updates(self):
        await self.watch()
        self.gateway.progress_jobs.return_value = [self.job(status='running')]
        self.bot.edit_message_text.side_effect = TelegramRetryAfter(
            method=EditMessageText(text='test', chat_id=5, message_id=40),
            message='flood', retry_after=300,
        )
        await self.progress.poll_once()
        await self.progress.poll_once()
        self.bot.edit_message_text.assert_awaited_once()
        self.assertFalse(self.repository.records['image-1']['finished'])
