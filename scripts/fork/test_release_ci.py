#!/usr/bin/env python3
"""Exercise the deployment authority boundary with GitHub API responses."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from release_ci import CI_PATH, PREPARE_PATH, REPOSITORY, resolve_delivery, verify_ci, verify_delivery


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
