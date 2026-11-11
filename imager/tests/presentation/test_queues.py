"""Offline regressions for the transport state machines.

Load the real function bodies with explicit collaborators, so these tests never
initialize MongoDB, log files, a network listener or the application's mediator.
"""

import ast
import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[2]


def load_module(relative, collaborators):
    path = ROOT / relative
    syntax = ast.parse(path.read_text(encoding='utf-8'))
    syntax.body = [node for node in syntax.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    namespace = dict(collaborators)
    exec(compile(syntax, str(path), 'exec'), namespace)
    return namespace


class HttpError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


def worker_module(send):
    return load_module('src/presentation/api/workers.py', {
        'asyncio': asyncio,
        'HTTPException': HttpError,
        'settings': SimpleNamespace(api_queue_maxsize=1, api_shutdown_timeout_seconds=0.01),
        'GenerateImageCommand': lambda **values: SimpleNamespace(**values),
        'GenerateImageResponse': lambda **values: SimpleNamespace(**values),
        'mediator': SimpleNamespace(send=send),
        'presentation_logger': Mock(),
    })


def request(future):
    return dict(image_path='source.jpg', group_name='group', insertion_format='crop',
                alpha_channel=30, noise_level=15, cell_size=2, result_size=3,
                output_name=None, future=future, request_id='test-request')


def journal_class():
    return load_module('src/presentation/rabbitmq/rpc_journal.py', {
        'hashlib': hashlib, 'json': json, 'os': os, 'tempfile': tempfile,
        'time': time, 'Path': Path, 'presentation_logger': Mock(),
    })['RpcJournal']


class FakeMessage:
    def __init__(self, body, request_id='request-1', headers=None):
        self.body = json.dumps(body).encode('utf-8')
        self.correlation_id = request_id
        self.reply_to = 'reply-queue'
        self.headers = headers or {}
        self.expiration = 600
        self.ack = AsyncMock()
        self.nack = AsyncMock()
        self.reject = AsyncMock()


def consumer_module(artifact_root):
    return load_module('src/presentation/rabbitmq/consumer.py', {
        'asyncio': asyncio, 'json': json, 'Path': Path,
        'aio_pika': SimpleNamespace(
            IncomingMessage=object, abc=SimpleNamespace(AbstractExchange=object),
            Message=lambda **values: SimpleNamespace(**values),
            DeliveryMode=SimpleNamespace(PERSISTENT=2),
        ),
        'settings': SimpleNamespace(
            file_path_prefix='', generated_images_path=str(artifact_root),
            rabbitmq_requests_queue='requests', rabbitmq_max_retries=2,
        ),
        'RpcJournal': journal_class(), 'presentation_logger': Mock(),
        'error_from_exception': lambda error: SimpleNamespace(
            retryable=isinstance(error, (ConnectionError, TimeoutError)),
        ),
    })


class TestHttpQueue(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_active_request_does_not_kill_worker(self):
        entered, release = threading.Event(), threading.Event()

        def send(command):
            entered.set()
            release.wait(timeout=2)
            return SimpleNamespace(is_success=True, value='result.jpg')

        module = worker_module(send)
        queue = asyncio.Queue()
        first = asyncio.get_running_loop().create_future()
        await queue.put(request(first))
        task = asyncio.create_task(module['worker'](queue))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            first.cancel()
            release.set()
            second = asyncio.get_running_loop().create_future()
            await queue.put(request(second))
            response = await asyncio.wait_for(second, 1)
            self.assertEqual(response.path, 'result.jpg')
            self.assertFalse(task.done())
            await asyncio.wait_for(queue.join(), 1)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancelled_waiting_request_is_not_rendered(self):
        send = Mock(return_value=SimpleNamespace(is_success=True, value='result.jpg'))
        module = worker_module(send)
        queue = asyncio.Queue()
        future = asyncio.get_running_loop().create_future()
        future.cancel()
        await queue.put(request(future))
        task = asyncio.create_task(module['worker'](queue))
        try:
            await asyncio.wait_for(queue.join(), 1)
            send.assert_not_called()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_queue_full_is_rejected_without_unbounded_wait(self):
        class Router:
            def post(self, *args, **kwargs):
                return lambda function: function

            get = post

        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait('occupied')
        module = load_module('src/presentation/api/routers.py', {
            'asyncio': asyncio, 'uuid': uuid, 'HTTPException': HttpError,
            'APIRouter': Router, 'settings': SimpleNamespace(api_request_timeout_seconds=1),
            'GenerateImageRequest': object, 'GenerateImageResponse': object,
            'ListGroupsResponse': object, 'SyncGroupsResponse': object,
            'generation_queue': lambda: queue, 'accepting_requests': lambda: True,
            'presentation_logger': Mock(),
        })
        values = request(None)
        with self.assertRaises(HttpError) as raised:
            await module['generate_image'](SimpleNamespace(**values))
        self.assertEqual(raised.exception.status_code, 429)

    async def test_lifecycle_does_not_start_duplicate_workers(self):
        module = worker_module(Mock())
        module['start_workers']()
        module['start_workers']()
        self.assertEqual(len(module['_worker_tasks']), 1)
        await module['stop_workers']()
        self.assertEqual(module['_worker_tasks'], [])
        self.assertFalse(module['accepting_requests']())

    async def test_retryable_core_error_maps_to_service_unavailable(self):
        error = SimpleNamespace(retryable=True)
        module = worker_module(Mock(return_value=SimpleNamespace(is_success=False, error=error)))
        queue = asyncio.Queue()
        future = asyncio.get_running_loop().create_future()
        await queue.put(request(future))
        task = asyncio.create_task(module['worker'](queue))
        try:
            with self.assertRaises(HttpError) as raised:
                await asyncio.wait_for(future, 1)
            self.assertEqual(raised.exception.status_code, 503)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestRpcJournal(unittest.TestCase):
    def test_replay_requires_matching_request_and_existing_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = journal_class()(root)
            artifact = root / 'result.jpg'
            artifact.write_bytes(b'fixture')
            fingerprint = journal.fingerprint('generate_image', {'cell_size': 2})
            response = {'status': 'ok', 'data': {'path': artifact.name}}
            journal.store('request', fingerprint, response)
            self.assertEqual(journal.get('request', fingerprint), response)
            with self.assertRaises(ValueError):
                journal.get('request', 'different-parameters')
            artifact.unlink()
            self.assertIsNone(journal.get('request', fingerprint))

    def test_retention_bounds_entry_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = journal_class()(temporary, max_entries=2)
            for index in range(3):
                journal.store(str(index), 'fingerprint', {'status': 'error', 'error': 'invalid'})
            self.assertEqual(len(list(journal.directory.glob('*.json'))), 2)


class TestRabbitQueue(unittest.IsolatedAsyncioTestCase):
    async def test_completed_generation_is_replayed_without_rendering_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, 'result.jpg').write_bytes(b'fixture')
            module = consumer_module(temporary)
            handler = AsyncMock(return_value={'status': 'ok', 'data': {'path': 'result.jpg'}})
            module['_handle_request'] = handler
            exchange = SimpleNamespace(publish=AsyncMock())
            body = {'action': 'generate_image', 'payload': {'cell_size': 2}, 'request_id': 'same-request'}
            for _ in range(2):
                message = FakeMessage(body)
                await module['_process_message'](message, exchange)
                message.ack.assert_awaited_once()
            self.assertEqual(handler.await_count, 1)
            self.assertEqual(exchange.publish.await_count, 2)

    async def test_retryable_failure_republishes_persistent_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = consumer_module(temporary)
            module['_handle_request'] = AsyncMock(return_value={
                'status': 'error', 'error': 'database unavailable', 'retryable': True,
            })
            exchange = SimpleNamespace(publish=AsyncMock())
            message = FakeMessage({'action': 'list_groups', 'payload': {}})
            await module['_process_message'](message, exchange)
            publication = exchange.publish.await_args
            self.assertEqual(publication.kwargs['routing_key'], 'requests')
            self.assertEqual(publication.args[0].headers['x-imager-retry'], 1)
            self.assertEqual(publication.args[0].delivery_mode, 2)
            message.ack.assert_awaited_once()

    async def test_reply_loss_after_journaling_does_not_repeat_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, 'result.jpg').write_bytes(b'fixture')
            module = consumer_module(temporary)
            handler = AsyncMock(return_value={'status': 'ok', 'data': {'path': 'result.jpg'}})
            module['_handle_request'] = handler
            exchange = SimpleNamespace(publish=AsyncMock(side_effect=ConnectionError('reply lost')))
            body = {'action': 'generate_image', 'payload': {}, 'request_id': 'same-request'}
            first = FakeMessage(body)
            await module['_process_message'](first, exchange)
            first.ack.assert_awaited_once()
            exchange.publish = AsyncMock()
            await module['_process_message'](FakeMessage(body), exchange)
            self.assertEqual(handler.await_count, 1)

    async def test_retry_exhaustion_returns_explicit_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = consumer_module(temporary)
            module['_handle_request'] = AsyncMock(return_value={
                'status': 'error', 'error': 'database unavailable', 'retryable': True,
            })
            exchange = SimpleNamespace(publish=AsyncMock())
            message = FakeMessage({'action': 'list_groups', 'payload': {}}, headers={'x-imager-retry': 2})
            await module['_process_message'](message, exchange)
            response = json.loads(exchange.publish.await_args.args[0].body)
            self.assertEqual(response['code'], 'retry_exhausted')
            self.assertFalse(response['retryable'])
            message.ack.assert_awaited_once()

    async def test_expired_query_reply_does_not_requeue_forever(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = consumer_module(temporary)
            module['_handle_request'] = AsyncMock(return_value={'status': 'ok', 'data': {'groups': []}})
            exchange = SimpleNamespace(publish=AsyncMock(side_effect=ConnectionError('reply queue expired')))
            message = FakeMessage({'action': 'list_groups', 'payload': {}}, headers={'x-imager-retry': 2})
            await module['_process_message'](message, exchange)
            message.reject.assert_awaited_once_with(requeue=False)
            message.nack.assert_not_called()


class TestBotRpcRetry(unittest.IsolatedAsyncioTestCase):
    async def test_transport_retry_keeps_request_identifier(self):
        class AmqpError(Exception):
            pass

        path = ROOT.parent / 'telegram_bot/src/infrastructure/services/imager_service/imager_service.py'
        syntax = ast.parse(path.read_text(encoding='utf-8'))
        syntax.body = [node for node in syntax.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
        namespace = {
            'asyncio': asyncio, 'json': json, 'uuid': uuid, 'service_logger': Mock(),
            'aio_pika': SimpleNamespace(exceptions=SimpleNamespace(AMQPException=AmqpError)),
            'GenerateImageRequest': object, 'GenerateImageResponse': object,
            'ListGroupsResponse': object, 'SyncGroupsResponse': object,
        }
        exec(compile(syntax, str(path), 'exec'), namespace)
        service = namespace['ImagerService']('unused', 'requests')
        response = {'status': 'ok', 'data': {'path': 'result.jpg'}}
        service._rpc_once = AsyncMock(side_effect=[asyncio.TimeoutError(), response])
        self.assertEqual(await service._rpc('generate_image', {}), response)
        first, second = service._rpc_once.await_args_list
        self.assertEqual(first.args[2], second.args[2])


if __name__ == '__main__':
    unittest.main()
