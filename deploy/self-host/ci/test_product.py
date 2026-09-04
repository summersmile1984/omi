"""Actual process/resource ownership exercised without external services."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

from product import Fixture


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
