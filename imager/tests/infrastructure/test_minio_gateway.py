"""Compose isolation plus opt-in real-nginx behavior against a disposable stub.

No test starts Docker or contacts an application service. Compose tests only
render configuration with synthetic credentials. HTTP tests require a separate
gateway/stub fixture and IMAGER_MINIO_GATEWAY_TEST_ALLOW=isolated-stub.
Run this file with --serve-upstream inside the fixture's Python container to
provide the echo upstream named minio-storage, listening on port 9000.
"""
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[3]
INFRA = ROOT / 'infra'
STUB_MARKER = 'isolated-stub'
REPLICATION_HEADERS = (
    'X-Minio-Replication-Server-Side-Encryption-Sealed-Key',
    'X-Minio-Replication-Server-Side-Encryption-Seal-Algorithm',
    'X-Minio-Replication-Server-Side-Encryption-Iv',
    'X-Minio-Replication-Encrypted-Multipart',
    'X-Minio-Replication-Actual-Object-Size',
    'X-Minio-Replication-Ssec-Crc',
)


class MinioGatewayComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which('docker') is None:
            raise unittest.SkipTest('Docker Compose CLI is not installed; no daemon is required')

    def _configuration(self, filename):
        source = INFRA / filename
        names = set(re.findall(r'\$\{([A-Z][A-Z0-9_]*)', source.read_text(encoding='utf-8')))
        with tempfile.TemporaryDirectory(prefix='imager-compose-check-') as directory:
            scratch = Path(directory)
            environment = os.environ.copy()
            for name in names | {key for key in environment if key.startswith('COMPOSE_')}:
                environment.pop(name, None)
            values = {name: 'synthetic-fixture-value' for name in names}
            for name in names:
                if name.endswith(('_FILE', '_PATH', '_SOURCE', '_ROOT')):
                    values[name] = (scratch / name.lower()).as_posix()
            values.update(TEMPORAL_UI_PORT='18088', MINIO_CONSOLE_PORT='19001',
                          IMAGER_RUNTIME_UID='1000', IMAGER_RUNTIME_GID='1000',
                          TEMPORAL_HISTORY_SHARDS='16')
            env_file = scratch / 'compose.env'
            env_file.write_text(''.join(f"{name}='{value}'\n" for name, value in values.items()),
                                encoding='utf-8')
            result = subprocess.run(
                ['docker', 'compose', '-p', 'imager-minio-security-check',
                 '--env-file', str(env_file), '-f', str(source),
                 '--profile', 'bot', '--profile', 'tools', 'config', '--format', 'json'],
                env=environment, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_storage_has_no_direct_network_route_from_any_client(self):
        for filename in ('docker-compose.prod.yaml', 'docker-compose.dev.yaml'):
            with self.subTest(compose=filename):
                configuration = self._configuration(filename)
                services = configuration['services']
                storage, gateway = services['minio'], services['minio-gateway']
                self.assertEqual(set(storage['networks']), {'minio-backend'})
                self.assertIn('minio-storage', storage['networks']['minio-backend']['aliases'])
                self.assertTrue(configuration['networks']['minio-backend']['internal'])
                members = {name for name, service in services.items()
                           if 'minio-backend' in service.get('networks', {})}
                self.assertEqual(members, {'minio', 'minio-gateway'})
                self.assertEqual(set(gateway['networks']), {'default', 'minio-backend'})
                self.assertIn('minio', gateway['networks']['default']['aliases'])
                self.assertFalse(gateway.get('ports'))
                self.assertEqual([(port['host_ip'], port['target']) for port in storage['ports']],
                                 [('127.0.0.1', 9001)])
                self.assertEqual(gateway['image'], services['temporal-ui']['image'])
                self.assertEqual(int(gateway['mem_limit']), 64000000)
                self.assertTrue(any(volume['target'] == '/etc/nginx/nginx.conf'
                                    and volume.get('read_only') for volume in gateway['volumes']))
                for name in ('imager-dispatcher', 'imager-worker', 'catalog-import-worker',
                             'telegram-bot', 'import-catalog'):
                    dependencies = services[name]['depends_on']
                    self.assertNotIn('minio', dependencies)
                    self.assertEqual(dependencies['minio-gateway']['condition'], 'service_healthy')


class MinioGatewayHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        target = os.environ.get('IMAGER_MINIO_GATEWAY_TEST_URL', '')
        if os.environ.get('IMAGER_MINIO_GATEWAY_TEST_ALLOW') != STUB_MARKER or not target:
            raise unittest.SkipTest('Requires an explicitly enabled isolated gateway/stub fixture')
        cls.target = urlsplit(target)
        if (cls.target.scheme != 'http' or not cls.target.hostname or cls.target.username
                or cls.target.password or cls.target.path not in ('', '/')
                or cls.target.query or cls.target.fragment):
            raise ValueError('Expected an isolated HTTP gateway origin without credentials')
        status, response, headers = cls.request('GET', '/minio/health/ready')
        if status != 200 or headers.get('x-imager-test-upstream') != STUB_MARKER:
            raise RuntimeError('Refusing HTTP tests: upstream is not the isolated test stub')

    @classmethod
    def request(cls, method, path, headers=(), body=b''):
        connection = http.client.HTTPConnection(cls.target.hostname, cls.target.port or 80, timeout=10)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            connection.putheader('Host', 'minio:9000')
            connection.putheader('Content-Length', str(len(body)))
            for name, value in headers:
                connection.putheader(name, value)
            connection.endheaders(body)
            result = connection.getresponse()
            return result.status, result.read(), {key.lower(): value for key, value in result.getheaders()}
        finally:
            connection.close()

    def assert_blocked(self, method, path, headers=()):
        status, _, response_headers = self.request(method, path, headers)
        self.assertEqual(status, 403)
        self.assertNotIn('x-imager-test-upstream', response_headers)

    def test_unsigned_trailer_is_rejected_before_reaching_storage(self):
        for value in ('STREAMING-UNSIGNED-PAYLOAD-TRAILER',
                      ' \tstreaming-unsigned-payload-trailer\t ',
                      'unused, STREAMING-UNSIGNED-PAYLOAD-TRAILER'):
            with self.subTest(value=value):
                self.assert_blocked('PUT', '/bucket/object', [('x-amz-content-sha256', value)])
        self.assert_blocked('PUT', '/bucket/object', [
            ('X-Amz-Content-Sha256', 'UNSIGNED-PAYLOAD'),
            ('X-Amz-Content-Sha256', 'STREAMING-UNSIGNED-PAYLOAD-TRAILER'),
        ])

    def test_select_is_rejected_with_reordered_encoded_or_duplicated_query_keys(self):
        for query in ('select&select-type=2', 'select-type=2&select', 'other=1&select=&other=2',
                      'SELECT&SELECT-TYPE=2', 'sel%65ct&select-type=2',
                      '%73%65%6c%65%63%74&select-type=2',
                      '%53%45%4C%45%43%54&select-type=2',
                      'x=1&select%2dtype=2&x=2', 'select=one&select=two',
                      'select-type=2'):
            with self.subTest(query=query):
                self.assert_blocked('POST', '/bucket/object?' + query)

    def test_replication_metadata_cannot_be_supplied_in_query_or_form(self):
        for name in ('X-Minio-Replication-Server-Side-Encryption-Sealed-Key',
                     'x-minio-replication-ssec-crc',
                     '%58%2d%4d%69%6e%69%6f%2d%52%65%70%6c%69%63%61%74%69%6f%6e%2dSsec-Crc'):
            with self.subTest(name=name):
                self.assert_blocked('PUT', '/bucket/object?a=1&' + name + '=injected')
        for content_type in ('multipart/form-data; boundary=fixture',
                             'MULTIPART/FORM-DATA; boundary=fixture',
                             'application/x-www-form-urlencoded'):
            with self.subTest(content_type=content_type):
                self.assert_blocked('POST', '/bucket/object', [('Content-Type', content_type)])
                self.assert_blocked('PUT', '/bucket/object', [('Content-Type', content_type)])

    def test_dangerous_headers_are_removed_while_sdk_signature_and_metadata_survive(self):
        headers = [(name, 'injected') for name in REPLICATION_HEADERS]
        headers += [('X-Minio-Source-Replication-Request', 'true'),
                    ('X-Amz-Meta-Sha256', 'fixture-checksum'),
                    ('X-Amz-Content-Sha256', 'UNSIGNED-PAYLOAD'),
                    ('X-Amz-Date', '20260101T000000Z'),
                    ('Authorization', 'AWS4-HMAC-SHA256 synthetic-fixture-signature')]
        status, body, _ = self.request('PUT', '/bucket/object', headers, b'pixels')
        self.assertEqual(status, 200)
        upstream = json.loads(body)
        for name in REPLICATION_HEADERS:
            self.assertNotIn(name.lower(), upstream['headers'])
        self.assertEqual(upstream['headers']['host'], 'minio:9000')
        self.assertEqual(upstream['headers']['x-amz-meta-sha256'], 'fixture-checksum')
        self.assertEqual(upstream['headers']['authorization'], headers[-1][1])
        self.assertEqual(upstream['sha256'], hashlib.sha256(b'pixels').hexdigest())

    def test_sdk_multipart_paths_preserve_raw_uri_and_body(self):
        cases = (
            ('POST', '/bucket/object?uploads=', b'', 'application/octet-stream'),
            ('PUT', '/bucket/a%2Fb%20c.jpg?partNumber=1&uploadId=a%2Bb%2Fid', b'part', 'image/jpeg'),
            ('POST', '/bucket/object?uploadId=fixture', b'<CompleteMultipartUpload/>', 'application/xml'),
            ('DELETE', '/bucket/object?uploadId=fixture', b'', 'application/octet-stream'),
            ('GET', '/bucket/object?selectable=1&prefix=select', b'', 'application/octet-stream'),
        )
        for method, path, payload, content_type in cases:
            with self.subTest(method=method, path=path):
                status, body, _ = self.request(method, path, [('Content-Type', content_type)], payload)
                self.assertEqual(status, 200)
                upstream = json.loads(body)
                self.assertEqual(upstream['path'], path)
                self.assertEqual(upstream['sha256'], hashlib.sha256(payload).hexdigest())
        status, body, _ = self.request('PUT', '/bucket/object', [
            ('X-Amz-Content-Sha256', 'STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER'),
        ])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['headers']['x-amz-content-sha256'],
                         'STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER')


class StubUpstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def respond(self):
        length = int(self.headers.get('Content-Length', '0'))
        if length < 0 or length > 8 * 1024 * 1024:
            self.send_error(413)
            return
        payload = self.rfile.read(length)
        data = json.dumps({'path': self.path, 'method': self.command,
                           'sha256': hashlib.sha256(payload).hexdigest(),
                           'headers': {name.lower(): value for name, value in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header('X-Imager-Test-Upstream', STUB_MARKER)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = respond

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    if sys.argv[1:] == ['--serve-upstream']:
        ThreadingHTTPServer(('0.0.0.0', 9000), StubUpstream).serve_forever()
    else:
        unittest.main()
