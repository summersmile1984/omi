#!/usr/bin/env python3
"""Exercise delivery orchestration and the actual complete-manifest selector."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml

from prepare_release import ROOT, build_plan, prepare, sha256


class SelectionTests(unittest.TestCase):
    def test_release_selects_product_contracts_even_without_a_diff(self):
        for full in ('true', 'false'):
            with self.subTest(full=full):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / 'scripts/fork/run_checks.py'),
                        '--lane',
                        'ci',
                        '--base',
                        'HEAD',
                        '--platform',
                        'linux',
                        '--output',
                        'json',
                    ],
                    env={**os.environ, 'FORK_FULL_CHECKS': full},
                    capture_output=True,
                    text=True,
                    check=True,
                )
                selected = {check['id'] for check in json.loads(result.stdout)['checks']}
                if full == 'true':
                    self.assertTrue(
                        {
                            'fork-cloudflare-product-core',
                            'fork-selfhost-product-core',
                            'fork-cloudflare-routes',
                            'fork-selfhost-startup',
                        }
                        <= selected
                    )
                    self.assertNotIn('fork-macos-native-compile', selected)
                else:
                    self.assertNotIn('fork-cloudflare-product-core', selected)
                    self.assertNotIn('fork-selfhost-product-core', selected)

    def test_release_selects_native_contracts_in_the_native_job(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / 'scripts/fork/run_checks.py'),
                '--lane',
                'ci',
                '--base',
                'HEAD',
                '--platform',
                'macos',
                '--exclusive-platform',
                '--output',
                'json',
            ],
            env={**os.environ, 'FORK_FULL_CHECKS': 'true'},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            {check['id'] for check in json.loads(result.stdout)['checks']},
            {'fork-macos-native-identity', 'fork-macos-native-compile'},
        )


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='fork-delivery-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'source'
        brand = self.root / 'brand/example/manifest.yaml'
        brand.parent.mkdir(parents=True)
        manifest = yaml.safe_load((ROOT / 'brand/eddy/manifest.yaml').read_text())
        manifest['brand']['id'] = 'example'
        manifest.pop('self_hosted_inference')
        brand.write_text(json.dumps(manifest))
        self.output = self.root.parent / 'delivery'
        self.plan = build_plan(
            self.root, self.output, self.root.parent / 'inventory.json', 'example', 'production', 'a' * 40, 'b' * 40
        )
        self.commands = []
        self.failed_build = False
        self.wrong_image = False
        self.dirty = False

    def run_command(self, arguments, *, root, capture=False):
        self.assertEqual(root, self.root)
        self.commands.append(arguments)
        if arguments[:3] == ['git', 'rev-parse', 'HEAD']:
            return self.plan['commit']
        if arguments[:2] == ['git', 'status']:
            return ' M backend/main.py' if self.dirty else ''
        if arguments[:2] == ['node', 'deploy/cloudflare/scripts/release.mjs'] and arguments[2] == 'prepare':
            candidate = self.output / 'cloudflare/candidate.json'
            candidate.parent.mkdir()
            candidate.write_text(
                json.dumps(
                    {
                        'source': {'commit': self.plan['commit'], 'tree': self.plan['tree']},
                        'candidate_digest': 'c' * 64,
                        'pending': ['deployed qualification'],
                    }
                )
            )
        if arguments[:2] == ['docker', 'build'] and self.failed_build:
            raise subprocess.CalledProcessError(1, arguments)
        if arguments[:3] == ['docker', 'image', 'save']:
            (self.output / 'server-images.tar').write_bytes(b'image archive')
        if arguments[:2] == ['git', 'archive']:
            (self.output / 'source.tar.gz').write_bytes(b'source archive')
        if arguments[1:3] == ['scripts/fork/release_archive.py', 'pack']:
            (self.output / 'cloudflare.tar.gz').write_bytes(b'frozen candidate archive')
        if arguments[:3] == ['docker', 'image', 'inspect']:
            return json.dumps(
                [
                    {
                        'Id': 'sha256:' + 'd' * 64,
                        'Os': 'linux',
                        'Architecture': 'arm64' if self.wrong_image else 'amd64',
                        'Config': {
                            'Labels': {
                                'com.omi.source.git-commit': self.plan['commit'],
                                'com.omi.source.git-tree': self.plan['tree'],
                            }
                        },
                    }
                ]
            )
        return ''

    def test_archives_are_bound_to_one_source_after_normal_candidate_verification(self):
        receipt = prepare(self.plan, self.root, self.output, self.run_command)
        self.assertEqual(receipt['files']['server-images.tar'], sha256(self.output / 'server-images.tar'))
        self.assertEqual(set(receipt['images']), {'backend', 'auth', 'llm', 'web'})
        self.assertFalse(receipt['release_ready'])
        self.assertEqual(self.commands[-1][2], 'check')
        self.assertFalse(any('push' in command or 'apply' in command for command in self.commands))

    def test_failed_build_or_wrong_architecture_never_emits_delivery_receipt(self):
        for field, error in [('failed_build', subprocess.CalledProcessError), ('wrong_image', ValueError)]:
            with self.subTest(failure=field):
                self.output = self.root.parent / field
                self.plan = build_plan(
                    self.root,
                    self.output,
                    self.root.parent / 'inventory.json',
                    'example',
                    'production',
                    'a' * 40,
                    'b' * 40,
                )
                setattr(self, field, True)
                with self.assertRaises(error):
                    prepare(self.plan, self.root, self.output, self.run_command)
                self.assertFalse((self.output / 'delivery.json').exists())
                setattr(self, field, False)

    def test_dirty_source_fails_before_creating_artifacts(self):
        self.dirty = True
        with self.assertRaisesRegex(ValueError, 'commit source'):
            prepare(self.plan, self.root, self.output, self.run_command)
        self.assertFalse(self.output.exists())

    def test_mimo_freezes_only_selected_application_images(self):
        manifest_path = self.root / 'brand/example/manifest.yaml'
        manifest = json.loads(manifest_path.read_text())
        manifest['self_hosted_inference'] = {'production': 'mimo-cn'}
        manifest_path.write_text(json.dumps(manifest))
        self.plan = build_plan(
            self.root, self.output, self.root.parent / 'inventory.json', 'example', 'production', 'a' * 40, 'b' * 40
        )
        receipt = prepare(self.plan, self.root, self.output, self.run_command)
        self.assertEqual(set(receipt['images']), {'backend', 'auth', 'web'})
        self.assertFalse(any('deploy/self-host/Dockerfile.llm' in cmd for cmd in self.commands))


if __name__ == '__main__':
    unittest.main()
