#!/usr/bin/env python3
"""Exercise the deployment authority boundary with GitHub API responses."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

from release_ci import CI_PATH, PREPARE_PATH, REPOSITORY, resolve_delivery, verify_ci, verify_delivery

ROOT = Path(__file__).resolve().parents[2]


class CloudflareQualificationToolsTests(unittest.TestCase):
    """PR #14 omitted the interpreter on a clean CD checkout (run 34358400820)."""

    def run_step(self, fail=False):
        workflow = yaml.safe_load((ROOT / '.github/workflows/fork-cd-cloudflare.yml').read_text())
        step = next(
            row
            for row in workflow['jobs']['deploy']['steps']
            if row.get('name') == 'Prepare Cloudflare HTTP contract Python'
        )
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            probe = directory / 'contracts/deployment/core.py'
            probe.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / 'contracts/deployment/core.py', probe)
            interpreter = directory / 'deploy/cloudflare/python/api-core/.venv/bin/python'
            self.assertFalse(interpreter.exists())
            bin_dir = directory / 'bin'
            bin_dir.mkdir()
            # The package/Python downloader is the controlled seam. The step
            # creates a real venv and executes the unchanged standard-library
            # HTTP probe; CI never downloads a Python distribution from this test.
            provisioner = directory / 'provisioner.py'
            provisioner.write_text(
                'import sys, venv\n'
                'assert sys.argv[1:] == ["uv==0.12.3", "venv", "--python", "3.14", '
                '"deploy/cloudflare/python/api-core/.venv"]\n'
                + (
                    'raise SystemExit(47)\n'
                    if fail
                    else 'venv.EnvBuilder(with_pip=False, symlinks=True).create(sys.argv[-1])\n'
                )
            )
            launcher = bin_dir / 'uvx'
            launcher.write_text(
                '#!/bin/bash\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(provisioner)) + ' "$@"\n'
            )
            launcher.chmod(0o700)
            result = subprocess.run(
                ['bash', '-e', '-o', 'pipefail', '-c', step['run']],
                cwd=directory,
                env={**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']},
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result, interpreter.exists()

    def test_clean_cd_checkout_provisions_and_executes_the_real_http_probe(self):
        result, exists = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(exists)
        self.assertIn('--metadata', result.stdout)
        self.assertIn('--remote', result.stdout)

    def test_provisioning_failure_stops_before_running_the_probe(self):
        result, exists = self.run_step(fail=True)
        self.assertEqual(result.returncode, 47)
        self.assertFalse(exists)
        self.assertEqual(result.stdout, '')


class ReleaseAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.sha = 'a' * 40
        self.run = dict(
            repository={'full_name': REPOSITORY},
            head_repository={'full_name': REPOSITORY},
            path=CI_PATH,
            event='workflow_dispatch',
            status='completed',
            conclusion='success',
            head_sha=self.sha,
            run_attempt=1,
        )
        self.jobs = [
            {'name': name, 'conclusion': 'success'}
            for name in ('Fork gate (Server OS + Cloudflare)', 'Fork macOS native contracts')
        ]
        self.artifact = {'name': f'delivery-{self.sha}-eddy-beta', 'expired': False}

    def api(self, path):
        if '/jobs?' in path:
            return {'jobs': self.jobs}
        if path.startswith('compare/'):
            return {'status': 'ahead'}
        if '/artifacts?' in path:
            return {'artifacts': [self.artifact]}
        return self.run

    def test_full_same_source_ci_passes(self):
        self.assertEqual(verify_ci(1, self.sha, self.api)['commit'], self.sha)

    def test_partial_foreign_failed_or_stale_run_refused(self):
        for field, value in [
            ('event', 'push'),
            ('conclusion', 'failure'),
            ('head_sha', 'b' * 40),
            ('head_repository', {'full_name': 'attacker/omi'}),
        ]:
            with self.subTest(field=field):
                original = self.run[field]
                self.run[field] = value
                with self.assertRaises(ValueError):
                    verify_ci(1, self.sha, self.api)
                self.run[field] = original
        self.jobs[1]['conclusion'] = 'skipped'
        with self.assertRaises(ValueError):
            verify_ci(1, self.sha, self.api)

    def test_delivery_run_must_have_correct_stage_artifact(self):
        self.run['path'] = PREPARE_PATH
        self.assertEqual(resolve_delivery(2, 'beta', self.api)['sha'], self.sha)
        with self.assertRaises(ValueError):
            resolve_delivery(2, 'production', self.api)

    def test_archive_tampering_stops_before_deployment(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            archive = directory / 'server-images.tar'
            archive.write_bytes(b'accepted image bytes')
            receipt = dict(
                commit=self.sha,
                stage='beta',
                brand='eddy',
                ci_run_id=1,
                files={'server-images.tar': hashlib.sha256(archive.read_bytes()).hexdigest()},
            )
            (directory / 'delivery.json').write_text(json.dumps(receipt))
            verify_delivery(directory, self.sha, 'beta', 'self_hosted', self.api)
            archive.write_bytes(b'different images')
            with self.assertRaises(ValueError):
                verify_delivery(directory, self.sha, 'beta', 'self_hosted', self.api)


if __name__ == '__main__':
    unittest.main()
