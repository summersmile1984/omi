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

from product import Fixture, ROOT, render
from loopback import handler as proxy_handler


def mimo_secret(directory):
    from fork.operator_ai import MiMo

    path = Path(directory) / 'mimo-secret.json'
    path.write_text(json.dumps({'MIMO_API_KEY': 'synthetic', 'MIMO_BASE_URL': MiMo().base_url}))
    return path


class FixtureHTTP(unittest.TestCase):
    def test_options_preserves_upstream_cors_approval_and_denial(self):
        seen = []
        allowed_origin = 'http://127.0.0.1:34900'

        class CorsOwner(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_OPTIONS(self):
                request = (
                    self.path,
                    self.headers.get('Origin'),
                    self.headers.get('Access-Control-Request-Method'),
                    self.headers.get('Access-Control-Request-Headers'),
                )
                seen.append(request)
                allowed = request[1] == allowed_origin
                body = b'' if allowed else b'origin rejected'
                self.send_response(204 if allowed else 403)
                if allowed:
                    self.send_header('Access-Control-Allow-Origin', allowed_origin)
                    self.send_header('Access-Control-Allow-Methods', 'POST')
                    self.send_header('Access-Control-Allow-Headers', 'authorization,content-type')
                    self.send_header('Vary', 'Origin')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with ExitStack() as stack:
            upstream = ThreadingHTTPServer(('127.0.0.1', 0), CorsOwner)
            proxy = ThreadingHTTPServer(('127.0.0.1', 0), proxy_handler('127.0.0.1', upstream.server_port))
            for server in (upstream, proxy):
                thread = threading.Thread(target=lambda server=server: server.serve_forever(poll_interval=0.01))
                thread.start()
                stack.callback(server.server_close)
                stack.callback(thread.join, 5)
                stack.callback(server.shutdown)
            for origin in (allowed_origin, 'https://untrusted.example.invalid'):
                with closing(HTTPConnection('127.0.0.1', proxy.server_port, timeout=3)) as client:
                    client.request(
                        'OPTIONS',
                        '/v2/messages',
                        headers={
                            'Origin': origin,
                            'Access-Control-Request-Method': 'POST',
                            'Access-Control-Request-Headers': 'authorization,content-type',
                        },
                    )
                    response = client.getresponse()
                    self.assertEqual(response.status, 204 if origin == allowed_origin else 403)
                    self.assertEqual(
                        response.getheader('Access-Control-Allow-Origin'),
                        allowed_origin if origin == allowed_origin else None,
                    )
                    self.assertEqual(response.read(), b'' if origin == allowed_origin else b'origin rejected')
            self.assertEqual(
                seen,
                [
                    ('/v2/messages', origin, 'POST', 'authorization,content-type')
                    for origin in (allowed_origin, 'https://untrusted.example.invalid')
                ],
            )

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
                Fixture(
                    output,
                    'fixture-proof',
                    34800,
                    model_stores={'embedding': output},
                    mimo_secret_file=mimo_secret(directory),
                )
            self.assertEqual(marker.read_text(), 'retained')

    def test_actual_http_suite_cancellation_uses_the_owned_process_boundary(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as stalled_auth:
            stalled_auth.bind(('127.0.0.1', 0))
            stalled_auth.listen(1)
            stalled_auth.settimeout(10)
            fixture = Fixture(
                Path(directory) / 'owned',
                'fixture-proof',
                34800,
                model_stores={'embedding': Path(directory)},
                mimo_secret_file=mimo_secret(directory),
            )
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
            fixture = Fixture(
                Path(directory) / 'owned',
                'fixture-proof',
                34800,
                model_stores={'embedding': Path(directory)},
                mimo_secret_file=mimo_secret(directory),
            )
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
    def test_mimo_requires_local_embedding_and_rejects_ambiguous_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / 'secret.json'
            from fork.operator_ai import MiMo

            secret.write_text(json.dumps({'MIMO_API_KEY': 'synthetic', 'MIMO_BASE_URL': MiMo().base_url}))
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

    def test_model_capacity_rejects_the_observed_oom_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stores = {'embedding': root}
            services = {'embedding': {'mem_limit': '4294967296'}}
            fixture = Fixture(
                root / 'models',
                'fixture-models',
                34800,
                model_stores=stores,
                mimo_secret_file=mimo_secret(directory),
            )
            calls = []

            def engine_info(args, **kwargs):
                calls.append(args)
                return '8318562304\n'

            fixture.command = engine_info
            with self.assertRaisesRegex(RuntimeError, 'at least 8 GiB Docker memory'):
                fixture.admit_model_capacity(services)
            self.assertEqual(calls, [['docker', 'info', '--format', '{{.MemTotal}}']])
            self.assertFalse(fixture.created)
            self.assertFalse(json.loads((fixture.output / 'model-capacity.json').read_text())['admitted'])
            fixture.command = lambda *args, **kwargs: str(16 * 1024**3)
            fixture.admit_model_capacity(services)
            self.assertTrue(json.loads((fixture.output / 'model-capacity.json').read_text())['admitted'])

    def test_partial_or_missing_model_stores_fail_before_creating_fixture_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = mimo_secret(directory)
            for stores in (
                None,
                {},
                {'speech': root},
                {'embedding': root, 'llm': None, 'speech': root},
                {'embedding': root, 'llm': root, 'speech': root / 'missing'},
            ):
                with self.subTest(stores=stores), self.assertRaises(ValueError):
                    Fixture(root / 'output', 'fixture-models', 34800, model_stores=stores, mimo_secret_file=secret)
                self.assertFalse((root / 'output').exists())

    def test_mimo_missing_embedding_or_invalid_credential_fails_before_creating_state(self):
        from fork.operator_ai import MiMo

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / 'secret.json'
            for credential, stores in (
                ({'MIMO_API_KEY': 'synthetic', 'MIMO_BASE_URL': MiMo().base_url}, None),
                ({'MIMO_BASE_URL': MiMo().base_url}, {'embedding': root}),
                ({'MIMO_API_KEY': 'synthetic', 'MIMO_BASE_URL': 'https://example.invalid'}, {'embedding': root}),
            ):
                secret.write_text(json.dumps(credential))
                with self.subTest(credential=list(credential), stores=stores), self.assertRaises(ValueError):
                    Fixture(root / 'output', 'fixture-mimo', 34800, model_stores=stores, mimo_secret_file=secret)
                self.assertFalse((root / 'output').exists())

    def test_prepare_preserves_complete_rendered_capabilities_and_real_embedding_service(self):
        from fork.operator_ai import MiMo

        # A native local model is refused at admission, so the admitted shape
        # is the hosted operator credential plus the local embedding store.
        for operator in ('mimo-cn',):
            with self.subTest(operator=operator), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                secret = root / 'secret.json'
                secret.write_text(json.dumps({'MIMO_API_KEY': 'synthetic', 'MIMO_BASE_URL': MiMo().base_url}))
                stores = {'embedding': root}
                fixture = Fixture(
                    root / 'output',
                    'fixture-models',
                    34800,
                    model_stores=stores,
                    mimo_secret_file=secret,
                )
                # Only Docker IO is controlled. Profile resolution and fixture
                # preparation execute normally, retaining every capability.
                services = render.load_yaml(ROOT / 'deploy/self-host/compose.production.yml')['services']
                for service in services.values():
                    service['environment'] = {}
                    if 'mem_limit' in service:
                        service['mem_limit'] = str(4 * 1024**3)

                def command(args, **kwargs):
                    if args[:2] == ['docker', 'info']:
                        return str(16 * 1024**3)
                    raise AssertionError(f'unexpected docker call: {args[:2]}')

                fixture.command = command
                # The fixture now calls selected_config() instead of docker
                # compose config; hand it a graph that mirrors what a real
                # specialize() pass would emit for the MiMo shape.
                fixture.selected_config = lambda values: {'services': services}
                fixture.prepare()
                expected = render.resolve(
                    'self_hosted',
                    None,
                    ROOT / 'deploy/self-host/ci-rendered-product-manifest.json',
                    'local',
                    operator,
                )
                self.assertEqual(json.loads((fixture.output / 'profile.json').read_text()), expected)
                profile = expected['profiles']['self_hosted.local']
                self.assertNotEqual(profile['capabilities']['llm_provider'], 'disabled')
                self.assertTrue(profile['capabilities']['stt_providers'])
                self.assertNotEqual(profile['capabilities']['tts_provider'], 'disabled')
                composed = json.loads(fixture.compose_file.read_text())['services']
                self.assertEqual(composed['embedding']['image'], services['embedding']['image'])
                self.assertIn('embedding-artifact-check', composed)
                for name in ('backend', 'memory-maintenance-worker'):
                    self.assertIn('client', composed[name]['networks'])
                    self.assertEqual(composed[name]['environment']['MIMO_API_KEY'], 'synthetic')
                self.assertNotIn('MIMO_API_KEY', composed['queue-worker']['environment'])

    def test_operator_secret_file_replaces_native_llm_and_speech_for_hosted_providers(self):
        from fork.operator_ai import MiMo

        for operator in ('openrouter', 'cloudflare-gateway', 'siliconflow'):
            with self.subTest(operator=operator), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                secret = root / 'secret.json'
                # Match fork.operator_ai.CREDENTIAL_ENV for each provider; MiMo is
                # exercised by the prior subTest loop and intentionally excluded here.
                credential = {
                    'openrouter': {'OPENROUTER_API_KEY': 'synthetic'},
                    'cloudflare-gateway': {'CLOUDFLARE_API_TOKEN': 'synthetic'},
                    'siliconflow': {'SILICONFLOW_API_KEY': 'synthetic'},
                }[operator]
                secret.write_text(json.dumps(credential))
                fixture = Fixture(
                    root / 'output',
                    'fixture-models',
                    34800,
                    model_stores=None,
                    operator_secret_file=secret,
                    operator_provider=operator,
                )
                services = render.load_yaml(ROOT / 'deploy/self-host/compose.production.yml')['services']
                # specialize() drops the local embedding services for any
                # hosted operator; mirror that here so the test sees the same
                # graph the production admission surface will see.
                for name in ('embedding', 'embedding-artifact-check'):
                    services.pop(name, None)
                for service in services.values():
                    service['environment'] = {}
                    if 'mem_limit' in service:
                        service['mem_limit'] = str(4 * 1024**3)

                def command(args, **kwargs):
                    if args[:2] == ['docker', 'info']:
                        return str(16 * 1024**3)
                    raise AssertionError(f'unexpected docker call: {args[:2]}')

                fixture.command = command
                fixture.selected_config = lambda values: {'services': services}
                fixture.prepare()
                composed = json.loads(fixture.compose_file.read_text())['services']
                expected_env = {
                    'openrouter': 'OPENROUTER_API_KEY',
                    'cloudflare-gateway': 'CLOUDFLARE_API_TOKEN',
                    'siliconflow': 'SILICONFLOW_API_KEY',
                }[operator]
                for name in ('backend', 'memory-maintenance-worker'):
                    self.assertIn('client', composed[name]['networks'])
                    self.assertEqual(composed[name]['environment'][expected_env], 'synthetic')
                # Neither MiMo nor local model env vars should leak.
                self.assertNotIn('MIMO_API_KEY', composed['backend']['environment'])
                self.assertNotIn('MIMO_API_KEY', composed['queue-worker']['environment'])
                # A hosted operator owns embeddings; the local Ollama service
                # and its preflight artifact check must not appear in the
                # composed graph.
                self.assertNotIn('embedding', composed)
                self.assertNotIn('embedding-artifact-check', composed)

    def test_hosted_operator_rejects_a_local_embedding_store(self):
        """Hosted operator AI owns embeddings over the wire; the fixture must
        not be told to bind a local BGE-M3 store on top of that."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / 'secret.json'
            secret.write_text(json.dumps({'OPENROUTER_API_KEY': 'synthetic'}))
            with self.assertRaises(ValueError) as raised:
                Fixture(
                    root / 'output',
                    'fixture-models',
                    34800,
                    model_stores={'embedding': root},
                    operator_secret_file=secret,
                    operator_provider='openrouter',
                )
            self.assertIn('hosted operator AI owns embeddings', str(raised.exception))
            self.assertFalse((root / 'output').exists())

    def test_mimo_operator_rejects_omitted_embedding_store(self):
        """The MiMo shape still owns the local BGE-M3 embedding service."""
        from fork.operator_ai import MiMo

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / 'secret.json'
            secret.write_text(json.dumps({'MIMO_API_KEY': 'synthetic', 'MIMO_BASE_URL': MiMo().base_url}))
            with self.assertRaises(ValueError) as raised:
                Fixture(
                    root / 'output',
                    'fixture-models',
                    34800,
                    model_stores=None,
                    mimo_secret_file=secret,
                )
            self.assertIn('real-model fixture requires the embedding store', str(raised.exception))
            self.assertFalse((root / 'output').exists())

    def test_operator_secret_file_without_provider_rejects_before_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / 'secret.json'
            secret.write_text(json.dumps({'OPENROUTER_API_KEY': 'synthetic'}))
            with self.assertRaises(ValueError) as raised:
                Fixture(
                    root / 'output',
                    'fixture-models',
                    34800,
                    model_stores={'embedding': root},
                    operator_secret_file=secret,
                )
            self.assertIn('--operator-provider is required', str(raised.exception))

    def test_operator_and_mimo_secret_files_are_mutually_exclusive(self):
        from fork.operator_ai import MiMo

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mimo_secret = root / 'mimo.json'
            mimo_secret.write_text(json.dumps({'MIMO_BASE_URL': MiMo().base_url, 'MIMO_API_KEY': 'x'}))
            operator_secret = root / 'operator.json'
            operator_secret.write_text(json.dumps({'OPENROUTER_API_KEY': 'x'}))
            with self.assertRaises(ValueError) as raised:
                Fixture(
                    root / 'output',
                    'fixture-models',
                    34800,
                    model_stores={'embedding': root},
                    mimo_secret_file=mimo_secret,
                    operator_secret_file=operator_secret,
                    operator_provider='openrouter',
                )
            self.assertIn('not both', str(raised.exception))


    def test_every_hosted_credential_renders_an_admitted_profile_row(self):
        """Regression: the operator credential must reach render.resolve().

        The fixture used to pass operator_ai=None for every non-MiMo secret
        file, so the rendered self_hosted row kept its native `llm` and every
        backend container died at admission with 'remove row.llm'. Only the
        mimo-cn path ever reached configure(); this drives the actual
        manifest+render seam for all four providers plus the legacy mimo file.
        """
        from fork.operator_ai import MiMo

        cases = {
            'openrouter': ({'OPENROUTER_API_KEY': 'synthetic'}, 'openrouter'),
            'siliconflow': ({'SILICONFLOW_API_KEY': 'synthetic'}, 'siliconflow'),
            'cloudflare-gateway': ({'CLOUDFLARE_API_TOKEN': 'synthetic'}, 'cloudflare-gateway'),
            'mimo-cn': ({'MIMO_API_KEY': 'synthetic', 'MIMO_BASE_URL': MiMo().base_url}, 'mimo'),
        }
        for operator, (credential, row_provider) in cases.items():
            with self.subTest(operator=operator), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                secret = root / 'secret.json'
                secret.write_text(json.dumps(credential))
                # Hosted vendors own embeddings over the wire (no local store);
                # MiMo keeps its local BGE-M3 contract.
                stores = None if operator != 'mimo-cn' else {'embedding': root}
                fixture = Fixture(
                    root / 'output',
                    'fixture-models',
                    34800,
                    model_stores=stores,
                    operator_secret_file=secret,
                    operator_provider=operator,
                )
                table = fixture._render_profile(fixture._brand_manifest())
                profile = table['profiles']['self_hosted.local']
                self.assertEqual(profile['operator_ai']['provider'], row_provider)
                self.assertNotIn('llm', profile, 'native llm row survives -> admission refuses it')
                self.assertNotIn('speech', profile, 'native speech row survives the hosted selection')
                if operator == 'mimo-cn':
                    # MiMo keeps the local embedding contract; hosted vendors
                    # supply embeddings over the wire (configure() strips it).
                    self.assertIn('embedding', profile)
                else:
                    self.assertNotIn('embedding', profile)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Fixture(
                root / 'output',
                'fixture-models',
                34800,
                model_stores={'embedding': root},
                mimo_secret_file=mimo_secret(root),
            )
            table = fixture._render_profile(fixture._brand_manifest())
            profile = table['profiles']['self_hosted.local']
            self.assertEqual(profile['operator_ai']['provider'], 'mimo')
            self.assertNotIn('llm', profile)

    def test_local_stage_compose_uses_pgvector_and_pin(self):
        """SELF_HOST_STAGE=local shares PostgreSQL as the vector store.

        The fixture must build an env file that drives the production
        compose to (a) launch the pgvector build of PostgreSQL with the
        exact digest dev/docker-compose.dev.yml pins, (b) bind
        VECTOR_STORE_PROVIDER=pgvector, and (c) provide the
        PGVECTOR_COLLECTION_PREFIX bootstrap requires. Without these the
        fixture admission crashes on 'VECTOR_STORE_PROVIDER conflicts
        with the selected deployment profile'.
        """
        from fork.operator_ai import MiMo

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / 'secret.json'
            secret.write_text(json.dumps({'MIMO_API_KEY': 'synthetic', 'MIMO_BASE_URL': MiMo().base_url}))
            fixture = Fixture(
                root / 'output',
                'fixture-models',
                34800,
                model_stores={'embedding': root},
                mimo_secret_file=secret,
            )

            def command(args, **kwargs):
                if args[:2] == ['docker', 'info']:
                    return str(16 * 1024**3)
                raise AssertionError(f'unexpected docker call: {args[:2]}')

            fixture.command = command
            captured = {}

            def fake_selected_config(values):
                from model_services import specialize, profile_for
                import yaml as _yaml
                from pathlib import Path as _Path
                config = _yaml.safe_load((_Path(ROOT) / 'deploy/self-host/compose.production.yml').read_text())
                specialized = specialize(config, profile_for(values))
                captured['config'] = specialized
                # Snapshot the pgvector-migrate dependency wiring before the
                # fixture's prepare() post-processing strips depends_on from
                # the in-place service dicts; deep-copy preserves the data.
                captured['depends_on'] = {
                    name: dict(specialized['services'][name].get('depends_on') or {})
                    for name in ('backend', 'memory-maintenance-worker', 'queue-worker')
                }
                for service in specialized['services'].values():
                    if isinstance(service, dict) and 'mem_limit' in service:
                        service['mem_limit'] = str(4 * 1024**3)
                return specialized

            fixture.selected_config = fake_selected_config
            fixture.prepare()
            env_text = (fixture.output / 'fixture.env').read_text()
            self.assertIn(
                'POSTGRES_IMAGE=pgvector/pgvector:pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b',
                env_text,
            )
            self.assertIn('VECTOR_STORE_PROVIDER=pgvector', env_text)
            self.assertIn('PGVECTOR_COLLECTION_PREFIX=contract', env_text)
            specialized = captured['config']
            self.assertIn(
                '${POSTGRES_IMAGE:-postgres:16.4-alpine@sha256:',
                specialized['services']['postgres']['image'],
            )
            self.assertIn('pgvector-migrate', specialized['services'])
            for name in ('backend', 'memory-maintenance-worker', 'queue-worker'):
                depends = captured['depends_on'][name]
                self.assertEqual(
                    depends.get('pgvector-migrate', {}).get('condition'),
                    'service_completed_successfully',
                    f'{name}: must wait for pgvector-migrate before reading the schema',
                )

    def test_production_default_compose_keeps_alpine_postgres_and_qdrant(self):
        """Production default is byte-stable: bare postgres + qdrant.

        Fork-owned compose must not change the production default; the
        fixture only swaps them through POSTGRES_IMAGE / VECTOR_STORE_PROVIDER.
        """
        compose = render.load_yaml(ROOT / 'deploy/self-host/compose.production.yml')['services']
        # The pin is identical to the pre-override compose; the override
        # path wraps the same default with `${VAR:-default}` so a fixture
        # can override it without changing the production default.
        self.assertEqual(
            compose['postgres']['image'],
            '${POSTGRES_IMAGE:-postgres:16.4-alpine@sha256:5660c2cbfea50c7a9127d17dc4e48543eedd3d7a41a595a2dfa572471e37e64c}',
        )
        for service in ('backend', 'memory-maintenance-worker'):
            self.assertIn(
                'VECTOR_STORE_PROVIDER=${VECTOR_STORE_PROVIDER:-qdrant}',
                compose[service]['environment'],
                f'{service}: production default must default VECTOR_STORE_PROVIDER to qdrant',
            )


if __name__ == '__main__':
    unittest.main(verbosity=2)
