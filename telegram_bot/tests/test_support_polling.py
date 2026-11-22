"""A second poller must not survive its peer's shutdown or hide its failure."""
import asyncio
import unittest

from src.support.polling import poll_dispatchers


class FakeDispatcher:
    def __init__(self):
        self.started, self.stopped, self.release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.failure = None
        self.arguments = None
        self.stop_calls = 0

    async def start_polling(self, bot, **kwargs):
        self.arguments = bot, kwargs
        self.started.set()
        try:
            await self.release.wait()
            if self.failure:
                raise self.failure
        finally:
            self.stopped.set()

    async def stop_polling(self):
        self.stop_calls += 1
        self.release.set()
        await self.stopped.wait()


class SupportPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_shutdown_stops_support_and_keeps_independent_update_filters(self):
        main, support = FakeDispatcher(), FakeDispatcher()
        main_bot, support_bot = object(), object()
        task = asyncio.create_task(poll_dispatchers(
            main, main_bot, support_dispatcher=support, support_bot=support_bot,
        ))
        await asyncio.gather(main.started.wait(), support.started.wait())
        main.release.set()
        await asyncio.wait_for(task, 1)
        self.assertTrue(main.stopped.is_set() and support.stopped.is_set())
        self.assertEqual(support.stop_calls, 1)
        self.assertEqual(main.arguments[1]['allowed_updates'], ['message', 'callback_query'])
        self.assertEqual(support.arguments[1]['allowed_updates'], ['message'])
        self.assertTrue(main.arguments[1]['handle_signals'])
        self.assertFalse(support.arguments[1]['handle_signals'])
        self.assertFalse(support.arguments[1]['close_bot_session'])
        self.assertIs(support.arguments[0], support_bot)

    async def test_support_failure_stops_main_and_propagates_for_process_restart(self):
        main, support = FakeDispatcher(), FakeDispatcher()
        task = asyncio.create_task(poll_dispatchers(
            main, object(), support_dispatcher=support, support_bot=object(),
        ))
        await asyncio.gather(main.started.wait(), support.started.wait())
        support.failure = ValueError('Support startup failed')
        support.release.set()
        with self.assertRaisesRegex(ValueError, 'Support startup failed'):
            await asyncio.wait_for(task, 1)
        self.assertTrue(main.stopped.is_set())
        self.assertEqual(main.stop_calls, 1)

    async def test_cancellation_stops_both_pollers_through_their_public_api(self):
        main, support = FakeDispatcher(), FakeDispatcher()
        task = asyncio.create_task(poll_dispatchers(
            main, object(), support_dispatcher=support, support_bot=object(),
        ))
        await asyncio.gather(main.started.wait(), support.started.wait())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(main.stopped.is_set() and support.stopped.is_set())
        self.assertEqual((main.stop_calls, support.stop_calls), (1, 1))

    async def test_optional_support_keeps_one_main_poller(self):
        main = FakeDispatcher()
        task = asyncio.create_task(poll_dispatchers(main, object()))
        await main.started.wait()
        main.release.set()
        await asyncio.wait_for(task, 1)
        self.assertTrue(main.arguments[1]['handle_signals'])

    async def test_missing_support_identity_is_rejected_before_starting_polling(self):
        main, support = FakeDispatcher(), FakeDispatcher()
        with self.assertRaises(ValueError):
            await poll_dispatchers(main, object(), support_dispatcher=support)
        self.assertFalse(main.started.is_set() or support.started.is_set())
