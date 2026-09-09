#!/usr/bin/env python3
"""Exercise installed image identity and actual gateway rendering boundaries."""

import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import URLError
from deploy_server import verify_images, save, deploy, wait_ready
from release_ci import sha256_file
from server_gateway import gateway_config
from server_ai_acceptance import verify_chat_stream, verify_transcript
import base64


class ServerDeliveryTests(unittest.TestCase):
    def test_public_ai_requires_a_completed_payload_and_recognized_audio(self):
        terminal = base64.b64encode(json.dumps({'text': 'Synthetic answer'}).encode()).decode()
        self.assertEqual(verify_chat_stream('data: Synthetic answer\n\ndone: ' + terminal)['text'], 'Synthetic answer')
        self.assertEqual(
            verify_chat_stream('data: Synthetic answer__CRLF____CRLF__\n\ndone: ' + terminal)['text'],
            'Synthetic answer',
        )
        with self.assertRaises(RuntimeError):
            verify_chat_stream('data: Different answer\n\ndone: ' + terminal)
        for stream in ('data: Synthetic answer', 'done: ' + terminal, 'error: unavailable\n' + 'done: ' + terminal):
            with self.assertRaises(RuntimeError):
                verify_chat_stream(stream)
        verify_transcript({'stt_provider': 'mimo', 'transcript': '整理工作笔记'})
        with self.assertRaises(RuntimeError):
            verify_transcript({'stt_provider': 'mimo', 'transcript': 'unrelated response'})

    def setUp(self):
        self.image_id = 'sha256:' + 'c' * 64
        image_keys = {'backend': 'BACKEND_IMAGE', 'auth': 'AUTH_SERVER_IMAGE', 'web': 'WEB_IMAGE'}
        self.receipt = {
            'brand': 'eddy',
            'stage': 'beta',
            'commit': 'a' * 40,
            'tree': 'b' * 40,
            'platform': 'linux/amd64',
            'images': {role: {'image_id': self.image_id} for role in image_keys},
        }
        self.environment = {key: self.image_id for key in image_keys.values()}
        self.image = {
            'Id': self.image_id,
            'Os': 'linux',
            'Architecture': 'amd64',
            'Config': {
                'Labels': {
                    'com.omi.source.git-commit': self.receipt['commit'],
                    'com.omi.source.git-tree': self.receipt['tree'],
                }
            },
        }

    def inspect(self, args, capture):
        self.assertEqual(args, ['docker', 'image', 'inspect', self.image_id])
        self.assertTrue(capture)
        return json.dumps([self.image])

    def test_journal_write_is_private_and_replaceable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'journal.json'
            save(path, {'state': 'admitted'})
            save(path, {'state': 'deployed'})
            self.assertEqual(json.loads(path.read_text()), {'state': 'deployed'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(path.with_suffix('.tmp').exists())

    def test_readiness_retries_connections_but_has_a_finite_deadline(self):
        clock = [0]
        attempts = []

        def pause(seconds):
            clock[0] += seconds

        def request(url, timeout):
            attempts.append(url)
            if len(attempts) == 1:
                raise URLError('port forwarding is starting')
            response = MagicMock()
            response.__enter__.return_value = response
            response.getcode.return_value = 200
            response.geturl.return_value = url
            return response

        metadata = {'api_origin': 'https://api.example.com'}
        wait_ready(metadata, open_url=request, now=lambda: clock[0], pause=pause, timeout=4)
        self.assertEqual(attempts, ['https://api.example.com/ready'] * 2)
        clock[0] = 0
        with self.assertRaisesRegex(RuntimeError, 'deadline'):
            wait_ready(
                metadata,
                open_url=lambda *args, **kwargs: (_ for _ in ()).throw(URLError('unreachable')),
                now=lambda: clock[0],
                pause=pause,
                timeout=4,
            )
        self.assertEqual(clock[0], 4)

    def test_accepted_content_addressed_images(self):
        verify_images(self.receipt, self.environment, self.inspect)

    def test_image_roles_must_match_the_selected_profile(self):
        del self.receipt['images']['backend']
        with self.assertRaisesRegex(ValueError, 'image roles'):
            verify_images(self.receipt, self.environment, self.inspect)
        self.receipt['images']['backend'] = {'image_id': self.image_id}
        self.receipt['images']['llm'] = {'image_id': self.image_id}
        with self.assertRaisesRegex(ValueError, 'image roles'):
            verify_images(self.receipt, self.environment, self.inspect)

    def test_mutable_tag_or_wrong_source_rejected(self):
        self.environment['BACKEND_IMAGE'] = 'backend:latest'
        with self.assertRaises(ValueError):
            verify_images(self.receipt, self.environment, self.inspect)

        self.environment['BACKEND_IMAGE'] = self.image_id
        self.image['Config']['Labels']['com.omi.source.git-commit'] = 'd' * 40
        with self.assertRaises(ValueError):
            verify_images(self.receipt, self.environment, self.inspect)

    def test_failed_boot_cannot_mutate_live_service_or_advance_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delivery, destination = root / 'delivery', root / 'server'
            delivery.mkdir()
            destination.mkdir()
            (delivery / 'server-images.tar').write_bytes(b'accepted image archive')
            receipt = {
                **self.receipt,
                'stage': 'beta',
                'brand': 'eddy',
                'files': {'server-images.tar': sha256_file(delivery / 'server-images.tar')},
            }
            (delivery / 'delivery.json').write_text(json.dumps(receipt))
            values = {**self.environment, 'SELF_HOST_STAGE': 'beta'}
            (destination / 'runtime.env').write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
            pointer = {'commit': 'previous', 'release': str(root / 'previous')}
            save(destination / 'current.json', pointer)
            commands = []

            def execute(args, **kwargs):
                commands.append(args)
                return json.dumps([self.image]) if args[:3] == ['docker', 'image', 'inspect'] else ''

            with patch('deploy_server.run', side_effect=execute), patch(
                'deploy_server.boot_test', side_effect=RuntimeError('unhealthy image')
            ):
                with self.assertRaisesRegex(RuntimeError, 'unhealthy image'):
                    deploy(delivery, destination, 'colima-eddy-server')
            self.assertEqual(json.loads((destination / 'current.json').read_text()), pointer)
            self.assertFalse(any('backup' in command or 'deploy-images' in command for command in commands))
            journal = json.loads((destination / 'releases' / receipt['commit'] / 'journal.json').read_text())
            self.assertFalse(journal['release_ready'])
            self.assertEqual(journal['state'], 'failed-reconciliation-required')

    def test_accepted_release_advances_only_after_public_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delivery, destination = root / 'delivery', root / 'server'
            delivery.mkdir()
            destination.mkdir()
            (delivery / 'server-images.tar').write_bytes(b'accepted image archive')
            receipt = {
                **self.receipt,
                'stage': 'beta',
                'brand': 'eddy',
                'files': {'server-images.tar': sha256_file(delivery / 'server-images.tar')},
            }
            (delivery / 'delivery.json').write_text(json.dumps(receipt))
            (destination / 'runtime.env').write_text('SELF_HOST_STAGE=beta\n')
            pointer = {'commit': 'previous', 'release': str(root / 'previous')}
            save(destination / 'current.json', pointer)
            phases = []

            def execute(args, **kwargs):
                if args[:3] == ['docker', 'image', 'inspect']:
                    return json.dumps([self.image])
                if 'backup' in args or 'deploy-images' in args:
                    phases.append('backup' if 'backup' in args else 'deploy')
                    self.assertEqual(kwargs['env']['DOCKER_CONTEXT'], 'colima-eddy-server')
                if any(str(arg).endswith('server_gateway.py') for arg in args):
                    release = Path(args[-1])
                    save(release / 'metadata.json', {'api_origin': 'https://api.example.com'})
                if '--remote' in args:
                    self.assertEqual(json.loads((destination / 'current.json').read_text()), pointer)
                    phases.append('accept')
                if any(str(arg).endswith('server_ai_acceptance.py') for arg in args):
                    phases.append('ai')
                    self.assertEqual(json.loads((destination / 'current.json').read_text()), pointer)
                if 'exec' in args:
                    phases.append('embedding')
                return ''

            with patch('deploy_server.run', side_effect=execute), patch(
                'deploy_server.boot_test', side_effect=lambda *args: phases.append('boot')
            ), patch('deploy_server.wait_ready', side_effect=lambda *args: phases.append('ready')):
                deploy(delivery, destination, 'colima-eddy-server')
            self.assertEqual(phases, ['boot', 'backup', 'deploy', 'ready', 'accept', 'ai', 'embedding'])
            release = destination / 'releases' / receipt['commit']
            self.assertEqual(json.loads((destination / 'current.json').read_text())['release'], str(release))
            self.assertTrue(json.loads((release / 'journal.json').read_text())['release_ready'])

    def test_gateway_preserves_streaming_and_separates_authorities(self):
        profile = {f'{key}_base_url': f'https://server-{key}.example.com' for key in ('api', 'auth', 'web', 'objects')}
        nginx, compose, hosts = gateway_config(profile, 'nginx:1.28-alpine@sha256:' + 'd' * 64, self.image_id, 19080)
        self.assertEqual(len(hosts), 4)
        self.assertEqual(compose['services']['gateway']['ports'], ['127.0.0.1:19080:8080'])
        self.assertIn('proxy_buffering off;', nginx)
        self.assertIn('proxy_set_header Upgrade $http_upgrade;', nginx)
        self.assertIn('location ^~ /internal/ { return 404; }', nginx)
        profile['auth_base_url'] = profile['api_base_url']
        with self.assertRaises(ValueError):
            gateway_config(profile, 'nginx:1.28-alpine@sha256:' + 'd' * 64, self.image_id, 19080)


if __name__ == '__main__':
    unittest.main()
