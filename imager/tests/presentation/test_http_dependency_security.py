"""Exercise the real HTTP router and FastAPI parser without infrastructure/network."""
import ast
import asyncio
import json
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import APIRouter, FastAPI, HTTPException

from imager_config.loader import config
from src.presentation.api.schemas import (
    GenerateImageRequest, GenerateImageResponse, ListGroupsResponse, SyncGroupsResponse,
)


ROOT = Path(__file__).resolve().parents[2]


class CompletingQueue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)
        item['future'].set_result(GenerateImageResponse(path='generated/result.jpg'))


def create_isolated_app(queue):
    # Execute the actual route definitions with explicit collaborators. Importing
    # the production mediator would initialize unrelated infrastructure adapters.
    path = ROOT / 'src/presentation/api/routers.py'
    syntax = ast.parse(path.read_text(encoding='utf-8-sig'))
    syntax.body = [node for node in syntax.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    namespace = {
        '__name__': 'isolated_http_dependency_security',
        'asyncio': asyncio, 'uuid': uuid, 'config': config,
        'APIRouter': APIRouter, 'HTTPException': HTTPException,
        'GenerateImageRequest': GenerateImageRequest,
        'GenerateImageResponse': GenerateImageResponse,
        'ListGroupsResponse': ListGroupsResponse,
        'SyncGroupsResponse': SyncGroupsResponse,
        'settings': SimpleNamespace(api_request_timeout_seconds=1),
        'accepting_requests': lambda: True, 'generation_queue': lambda: queue,
        'mediator': Mock(), 'presentation_logger': Mock(),
        'LoadMissingGroupsCommand': Mock(), 'GetListImageGroupsQuery': Mock(),
    }
    exec(compile(syntax, str(path), 'exec'), namespace)
    app = FastAPI()
    app.include_router(namespace['router'], prefix='/api')
    return app


async def post_raw_asgi(app, content_type):
    body = json.dumps({'image_path': 'input.jpg', 'group_name': 'cats'}).encode('utf-8')
    headers = [(b'host', b'testserver'), (b'content-length', str(len(body)).encode('ascii'))]
    if content_type is not None:
        headers.append((b'content-type', content_type))
    scope = {
        'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'},
        'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
        'path': '/api/v1/generate-image', 'raw_path': b'/api/v1/generate-image',
        'query_string': b'', 'root_path': '', 'headers': headers,
        'server': ('testserver', 80), 'client': ('127.0.0.1', 12345),
    }
    request_messages = [{'type': 'http.request', 'body': body, 'more_body': False}]
    response_messages = []

    async def receive():
        if request_messages:
            return request_messages.pop(0)
        return {'type': 'http.disconnect'}

    async def send(message):
        response_messages.append(dict(message))

    await asyncio.wait_for(app(scope, receive, send), timeout=2)
    starts = [message for message in response_messages if message['type'] == 'http.response.start']
    if len(starts) != 1:
        raise AssertionError(f'Expected one ASGI response start, received {len(starts)}')
    response_body = b''.join(
        message.get('body', b'') for message in response_messages
        if message['type'] == 'http.response.body'
    )
    return starts[0]['status'], json.loads(response_body)


class TestHttpDependencySecurity(unittest.IsolatedAsyncioTestCase):
    async def test_json_content_type_reaches_real_generation_router_once(self):
        queue = CompletingQueue()
        status, body = await post_raw_asgi(create_isolated_app(queue), b'application/json')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'path': 'generated/result.jpg'})
        self.assertEqual(len(queue.items), 1)
        self.assertEqual(queue.items[0]['image_path'], 'input.jpg')
        self.assertEqual(queue.items[0]['group_name'], 'cats')

    async def test_missing_json_content_type_is_rejected_before_enqueue(self):
        queue = CompletingQueue()
        status, body = await post_raw_asgi(create_isolated_app(queue), None)
        self.assertTrue(400 <= status < 500, status)
        self.assertIn('detail', body)
        self.assertEqual(queue.items, [])

    async def test_plain_text_json_is_rejected_before_enqueue(self):
        queue = CompletingQueue()
        status, body = await post_raw_asgi(create_isolated_app(queue), b'text/plain')
        self.assertTrue(400 <= status < 500, status)
        self.assertIn('detail', body)
        self.assertEqual(queue.items, [])
