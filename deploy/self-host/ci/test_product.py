"""Actual process/resource ownership exercised without external services."""

from concurrent.futures import ThreadPoolExecutor
import json
from contextlib import ExitStack, closing
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from product import Fixture
from loopback import handler as proxy_handler


class FixtureHTTP(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
