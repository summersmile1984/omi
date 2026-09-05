"""Actual process/resource ownership exercised without external services."""

from concurrent.futures import ThreadPoolExecutor
import json
from contextlib import ExitStack, closing
from http.client import HTTPConnection, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

from product import Fixture, core_only_profile
from loopback import handler as proxy_handler


class FixtureHTTP(unittest.TestCase):
    def test_http10_complete_body_is_finalized_after_upstream_socket_closes(self):
        body = b'complete response'

        class ClosingResponse(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with ExitStack() as stack:
            upstream = ThreadingHTTPServer(('127.0.0.1', 0), ClosingResponse)
            proxy = ThreadingHTTPServer(('127.0.0.1', 0), proxy_handler('127.0.0.1', upstream.server_port))
            for server in (upstream, proxy):
                thread = threading.Thread(target=lambda server=server: server.serve_forever(poll_interval=0.01))
                thread.start()
                stack.callback(server.server_close)
                stack.callback(thread.join, 5)
                stack.callback(server.shutdown)
            with closing(HTTPConnection('127.0.0.1', proxy.server_port, timeout=3)) as client:
                client.request('GET', '/')
                response = client.getresponse()
                self.assertEqual(response.read(), body)

    def test_sse_progress_arrives_before_upstream_completion(self):
        observed = threading.Event()
        first = b'think: Searching memories\n\n'
        last = b'done: synthetic-terminal\n\n'

        class Events(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                self.wfile.write(f'{len(first):x}\r\n'.encode() + first + b'\r\n')
                self.wfile.flush()
                if self.path == '/truncated':
                    self.close_connection = True
                    return
                if not observed.wait(5):
                    self.close_connection = True
                    return
                self.wfile.write(f'{len(last):x}\r\n'.encode() + last + b'\r\n0\r\n\r\n')
                self.wfile.flush()

        with ExitStack() as stack:
            upstream = ThreadingHTTPServer(('127.0.0.1', 0), Events)
            proxy = ThreadingHTTPServer(('127.0.0.1', 0), proxy_handler('127.0.0.1', upstream.server_port))
            for server in (upstream, proxy):
                thread = threading.Thread(target=lambda server=server: server.serve_forever(poll_interval=0.01))
                thread.start()
                stack.callback(server.server_close)
                stack.callback(thread.join, 5)
                stack.callback(server.shutdown)
            with closing(HTTPConnection('127.0.0.1', proxy.server_port, timeout=3)) as client:
                client.request('GET', '/v2/messages')
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader('Content-Type'), 'text/event-stream')
                self.assertEqual(response.read(len(first)), first)
                observed.set()
                self.assertEqual(response.read(), last)
            with closing(HTTPConnection('127.0.0.1', proxy.server_port, timeout=3)) as client:
                client.request('GET', '/truncated')
                response = client.getresponse()
                self.assertEqual(response.read(len(first)), first)
                with self.assertRaises(IncompleteRead):
                    response.read()

    def test_websocket_preserves_buffered_frames_audio_and_upstream_auth_denial(self):
        # RFC 6455 frames over real sockets. The first client/server frame shares
        # one write with its HTTP headers, exposing HTTP-parser read-ahead loss.
        pcm = bytes(range(256)) * 400
        mask = b'\x12\x34\x56\x78'
        audio = b'\x82\xff' + struct.pack('!Q', len(pcm)) + mask
        audio += bytes(value ^ mask[index % 4] for index, value in enumerate(pcm))
        first = b'\x81\x82' + mask + bytes((ord('h') ^ mask[0], ord('i') ^ mask[1]))
        ready = b'\x81\x05ready'
        reply = b'\x82\x7f' + struct.pack('!Q', len(pcm)) + pcm
        close_frame = b'\x88\x02\x03\xe8'
        received = []
        peer_closed = threading.Event()

        class AudioInput(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.close_connection = True
                if self.headers.get('Authorization') != 'Bearer synthetic-session':
                    self.send_response(401)
                    self.send_header('Content-Length', '0')
                    self.send_header('WWW-Authenticate', 'Bearer')
                    self.end_headers()
                    return
                received.append((self.path, self.headers.get('Sec-WebSocket-Key')))
                self.connection.sendall(
                    b'HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n'
                    b'Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n' + ready
                )
                received.append(self.rfile.read(len(first)))
                received.append(self.rfile.read(len(audio)))
                self.connection.sendall(reply + close_frame)
                peer_closed.set()

        with ExitStack() as stack:
            upstream = ThreadingHTTPServer(('127.0.0.1', 0), AudioInput)
            proxy = ThreadingHTTPServer(('127.0.0.1', 0), proxy_handler('127.0.0.1', upstream.server_port))
            for server in (upstream, proxy):
                thread = threading.Thread(target=lambda server=server: server.serve_forever(poll_interval=0.01))
                thread.start()
                stack.callback(server.server_close)
                stack.callback(thread.join, 5)
                stack.callback(server.shutdown)
            headers = {
                'Connection': 'keep-alive, Upgrade',
                'Upgrade': 'websocket',
                'Sec-WebSocket-Version': '13',
                'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ==',
            }
            with closing(HTTPConnection('127.0.0.1', proxy.server_port, timeout=5)) as client:
                client.request('GET', '/v4/listen', headers=headers)
                response = client.getresponse()
                self.assertEqual(response.status, 401)
                self.assertEqual(response.getheader('WWW-Authenticate'), 'Bearer')
                response.read()
                self.assertFalse(received, 'proxy fabricated an authenticated upgrade')
            with socket.create_connection(('127.0.0.1', proxy.server_port), timeout=5) as client:
                headers.update(Host='127.0.0.1', Authorization='Bearer synthetic-session')
                wire = 'GET /v4/listen?sample_rate=16000 HTTP/1.1\r\n'
                wire += ''.join(f'{key}: {value}\r\n' for key, value in headers.items()) + '\r\n'
                client.sendall(wire.encode() + first)
                with client.makefile('rb') as reader:
                    self.assertTrue(reader.readline().startswith(b'HTTP/1.1 101 '))
                    response_headers = []
                    while (line := reader.readline()) != b'\r\n':
                        self.assertTrue(line)
                        response_headers.append(line.lower())
                    self.assertIn(b'upgrade: websocket\r\n', response_headers)
                    self.assertEqual(reader.read(len(ready)), ready)
                    client.sendall(audio)
                    self.assertEqual(reader.read(len(reply) + len(close_frame)), reply + close_frame)
                    self.assertEqual(reader.read(1), b'')
            self.assertTrue(peer_closed.wait(5))
            self.assertEqual(received, [('/v4/listen?sample_rate=16000', headers['Sec-WebSocket-Key']), first, audio])

    def test_json_framing_is_preserved_and_ambiguous_framing_never_reaches_auth(self):
        calls = []

        class AuthInput(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                calls.append(raw)
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
                body = json.dumps(
                    dict(
                        received=data,
                        transfer_encoding=self.headers.get('Transfer-Encoding'),
                        length_headers=len(self.headers.get_all('Content-Length', [])),
                    )
                ).encode()
                self.send_response(200 if data is not None else 400)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with ExitStack() as stack:
            auth = ThreadingHTTPServer(('127.0.0.1', 0), AuthInput)
            proxy = ThreadingHTTPServer(('127.0.0.1', 0), proxy_handler('127.0.0.1', auth.server_port))
            for server in (auth, proxy):
                thread = threading.Thread(target=lambda server=server: server.serve_forever(poll_interval=0.01))
                thread.start()
                stack.callback(server.server_close)
                stack.callback(thread.join, 5)
                stack.callback(server.shutdown)
            payload = dict(name='分块请求', email='framing@example.invalid', password='synthetic-fixture-only')
            raw = json.dumps(payload, ensure_ascii=False).encode()
            for chunked in (False, True):
                with self.subTest(chunked=chunked), closing(
                    HTTPConnection('127.0.0.1', proxy.server_port, timeout=5)
                ) as client:
                    client.request(
                        'POST',
                        '/api/auth/sign-up/email',
                        body=iter((raw[:13], raw[13:])) if chunked else raw,
                        headers={
                            'Content-Type': 'application/json',
                            **({} if chunked else {'content-length': str(len(raw))}),
                        },
                        encode_chunked=chunked,
                    )
                    response = client.getresponse()
                    result = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertEqual(result, dict(received=payload, transfer_encoding=None, length_headers=1))
            before = len(calls)
            with closing(HTTPConnection('127.0.0.1', proxy.server_port, timeout=5)) as client:
                client.request(
                    'POST',
                    '/api/auth/sign-up/email',
                    body=b'0\r\n\r\n',
                    headers={'Content-Length': '5', 'Transfer-Encoding': 'chunked'},
                )
                response = client.getresponse()
                response.read()
                self.assertEqual(response.status, 400)
            self.assertEqual(len(calls), before, 'ambiguous framing was forwarded to the auth owner')


class FixtureOwnership(unittest.TestCase):
    def test_existing_output_is_never_adopted_or_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            marker = output / 'existing-state'
            marker.write_text('retained')
            with self.assertRaises(FileExistsError):
                Fixture(output, 'fixture-proof', 34800)
            self.assertEqual(marker.read_text(), 'retained')

    def test_actual_http_suite_cancellation_uses_the_owned_process_boundary(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as stalled_auth:
            stalled_auth.bind(('127.0.0.1', 0))
            stalled_auth.listen(1)
            stalled_auth.settimeout(10)
            fixture = Fixture(Path(directory) / 'owned', 'fixture-proof', 34800)
            origin = f'http://127.0.0.1:{stalled_auth.getsockname()[1]}'
            (fixture.output / 'metadata.json').write_text(
                json.dumps(
                    dict(
                        api_origin=origin,
                        auth_origin=origin,
                        target='self_hosted',
                        brand_id='fixture-proof',
                        trace_dir=str(fixture.output),
                    )
                )
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(fixture.run_core)
                try:
                    connection, _ = stalled_auth.accept()
                    with connection:
                        connection.settimeout(10)
                        self.assertTrue(connection.recv(128).startswith(b'POST /api/auth/sign-up/email '))
                        child = fixture.active.pid
                        fixture.stop()
                        with self.assertRaisesRegex(RuntimeError, 'fixture command failed'):
                            future.result(timeout=10)
                finally:
                    fixture.stop()
            status = subprocess.run(
                ['ps', '-p', str(child), '-o', 'stat='], capture_output=True, text=True
            ).stdout.strip()
            self.assertTrue(not status or status.startswith('Z'), 'cancelled HTTP suite remained alive')

    def test_cancellation_kills_owned_descendants_and_prevents_later_startup(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as ready:
            ready.bind(('127.0.0.1', 0))
            ready.listen(1)
            ready.settimeout(10)
            fixture = Fixture(Path(directory) / 'owned', 'fixture-proof', 34800)
            code = '''
import signal,socket,subprocess,sys
child=subprocess.Popen([sys.executable,'-c','import signal; signal.pause()'])
with socket.create_connection(('127.0.0.1',int(sys.argv[1]))) as ready:
    ready.sendall(str(child.pid).encode())
signal.pause()
'''
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(fixture.command, [sys.executable, '-c', code, str(ready.getsockname()[1])])
                try:
                    connection, _ = ready.accept()
                    with connection:
                        child = int(connection.recv(32))
                finally:
                    fixture.stop()
                with self.assertRaisesRegex(RuntimeError, 'fixture command failed'):
                    future.result(timeout=10)
            status = subprocess.run(
                ['ps', '-p', str(child), '-o', 'stat='], capture_output=True, text=True
            ).stdout.strip()
            self.assertTrue(not status or status.startswith('Z'), 'cancelled fixture left a serving descendant')
            with self.assertRaisesRegex(RuntimeError, 'startup was cancelled'):
                fixture.command([sys.executable, '-c', 'raise AssertionError("must not start")'])


class FixtureProfile(unittest.TestCase):
    def test_mimo_mode_requires_only_local_embedding_and_rejects_ambiguous_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / 'secret.json'
            secret.write_text(json.dumps({'MIMO_API_KEY': 'synthetic'}))
            fixture = Fixture(
                root / 'mimo', 'fixture-mimo', 34800, model_stores={'embedding': root}, mimo_secret_file=secret
            )
            self.assertEqual(set(fixture.model_stores), {'embedding'})
            fixture.command = lambda *args, **kwargs: str(8 * 1024**3)
            fixture.admit_model_capacity({'embedding': {'mem_limit': '4294967296'}})
            report = json.loads((fixture.output / 'model-capacity.json').read_text())
            self.assertEqual(set(report['model_memory_limits']), {'embedding'})
            with self.assertRaises(ValueError):
                Fixture(
                    root / 'bad',
                    'fixture-mimo',
                    34800,
                    model_stores={'embedding': root, 'llm': root},
                    mimo_secret_file=secret,
                )
            self.assertFalse((root / 'bad').exists())

    def test_model_capacity_rejects_the_observed_oom_host_and_leaves_core_mode_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stores = {kind: root for kind in ('embedding', 'llm', 'speech')}
            services = {name: {'mem_limit': '4294967296'} for name in ('embedding', 'llm')}
            fixture = Fixture(root / 'models', 'fixture-models', 34800, model_stores=stores)
            calls = []

            def engine_info(args, **kwargs):
                calls.append(args)
                return '8318562304\n'

            fixture.command = engine_info
            with self.assertRaisesRegex(RuntimeError, 'at least 12 GiB Docker memory'):
                fixture.admit_model_capacity(services)
            self.assertEqual(calls, [['docker', 'info', '--format', '{{.MemTotal}}']])
            self.assertFalse(fixture.created)
            self.assertFalse(json.loads((fixture.output / 'model-capacity.json').read_text())['admitted'])
            fixture.command = lambda *args, **kwargs: str(16 * 1024**3)
            fixture.admit_model_capacity(services)
            self.assertTrue(json.loads((fixture.output / 'model-capacity.json').read_text())['admitted'])
            core = Fixture(root / 'core', 'fixture-core', 34800)
            core.command = lambda *args, **kwargs: self.fail('core mode acquired real-model resource requirements')
            core.admit_model_capacity({})

    def test_partial_or_missing_model_stores_fail_before_creating_fixture_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stores in (
                {'speech': root},
                {'embedding': root, 'llm': None, 'speech': root},
                {'embedding': root, 'llm': root, 'speech': root / 'missing'},
            ):
                with self.subTest(stores=stores), self.assertRaises(ValueError):
                    Fixture(root / 'output', 'fixture-models', 34800, model_stores=stores)
                self.assertFalse((root / 'output').exists())

    def test_core_contract_disables_unowned_media_and_llm_capabilities(self):
        profile = core_only_profile(
            {
                'llm': {'unvalidated': 'removed before admission'},
                'speech': {'unvalidated': 'removed before admission'},
                'capabilities': {
                    'llm_provider': 'ollama',
                    'stt_providers': ['sensevoice'],
                    'tts_provider': 'kokoro',
                    'push_provider': 'disabled',
                },
            }
        )

        self.assertNotIn('llm', profile)
        self.assertNotIn('speech', profile)
        self.assertEqual(profile['capabilities']['llm_provider'], 'disabled')
        self.assertEqual(profile['capabilities']['stt_providers'], [])
        self.assertEqual(profile['capabilities']['tts_provider'], 'disabled')


if __name__ == '__main__':
    unittest.main(verbosity=2)
