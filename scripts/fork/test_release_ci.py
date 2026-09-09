#!/usr/bin/env python3
"""Exercise the deployment authority boundary with GitHub API responses."""

import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml

from release_ci import CI_PATH, PREPARE_PATH, REPOSITORY, resolve_delivery, verify_ci, verify_delivery
from release_archive import pack_candidate, unpack_candidate

ROOT = Path(__file__).resolve().parents[2]


class FrozenArchiveTests(unittest.TestCase):
    def fixture(self, directory):
        directory.mkdir()
        content = b'accepted Worker module'
        payload = directory / 'workers/core/index.js'
        payload.parent.mkdir(parents=True)
        payload.write_bytes(content)
        body = {'artifact_files': {'workers': {'core/index.js': hashlib.sha256(content).hexdigest()}}}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        raw = json.dumps({**body, 'candidate_digest': digest}).encode()
        (directory / 'candidate.json').write_bytes(raw)
        return raw, digest, content

    def test_pack_transports_only_the_frozen_manifest_and_payload(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            candidate = root / 'candidate'
            _, digest, content = self.fixture(candidate)
            (candidate / 'resources').mkdir()
            (candidate / 'resources/python_modules').symlink_to('/absent/build/machine/dependencies')
            archive = root / 'candidate.tar.gz'
            pack_candidate(candidate, archive)
            with tarfile.open(archive) as bundle:
                self.assertEqual(bundle.getnames(), ['cloudflare/candidate.json', 'cloudflare/workers/core/index.js'])
            unpack_candidate(archive, root / 'unpacked', digest)
            self.assertEqual((root / 'unpacked/cloudflare/workers/core/index.js').read_bytes(), content)

    def fat_archive(self, archive, raw, content, mutation=None):
        with tarfile.open(archive, 'w:gz') as bundle:
            scratch = tarfile.TarInfo('cloudflare/resources/workers/api-core/python_modules')
            scratch.type = tarfile.SYMTYPE
            scratch.linkname = '/absent/build/machine/python_modules'
            bundle.addfile(scratch)
            for name, data in [('cloudflare/candidate.json', raw), ('cloudflare/workers/core/index.js', content)]:
                item = tarfile.TarInfo(name)
                if name.endswith('index.js') and mutation == 'missing':
                    continue
                if name.endswith('index.js') and mutation == 'link':
                    item.type = tarfile.SYMTYPE
                    item.linkname = '/tmp/foreign-owned-file'
                    bundle.addfile(item)
                    continue
                if name.endswith('index.js') and mutation == 'bytes':
                    data = b'changed module'
                item.size = len(data)
                bundle.addfile(item, io.BytesIO(data))
                if name.endswith('index.js') and mutation == 'duplicate':
                    bundle.addfile(item, io.BytesIO(data))

    def test_extract_ignores_unowned_build_links_but_verifies_every_frozen_file(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            raw, digest, content = self.fixture(root / 'candidate')
            archive = root / 'candidate.tar.gz'
            self.fat_archive(archive, raw, content)
            unpack_candidate(archive, root / 'unpacked', digest)
            self.assertEqual((root / 'unpacked/cloudflare/workers/core/index.js').read_bytes(), content)
            self.assertFalse((root / 'unpacked/cloudflare/resources').exists())

    def test_declared_links_missing_duplicates_and_changed_bytes_are_rejected(self):
        for mutation in ['link', 'missing', 'duplicate', 'bytes']:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as work:
                root = Path(work)
                raw, digest, content = self.fixture(root / 'candidate')
                archive = root / 'candidate.tar.gz'
                self.fat_archive(archive, raw, content, mutation)
                with self.assertRaises(ValueError):
                    unpack_candidate(archive, root / 'unpacked', digest)

    def test_archive_identity_must_match_the_admitted_receipt(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            raw, _, content = self.fixture(root / 'candidate')
            archive = root / 'candidate.tar.gz'
            self.fat_archive(archive, raw, content)
            with self.assertRaisesRegex(ValueError, 'candidate digest'):
                unpack_candidate(archive, root / 'unpacked', 'a' * 64)
            self.assertFalse((root / 'unpacked').exists())


class AdmissionSourceTests(unittest.TestCase):
    def test_both_cd_workflows_load_admission_from_the_workflow_revision(self):
        for target in ['cloudflare', 'server']:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as work:
                root = Path(work)
                source = root / 'checkout'
                source.mkdir()
                environment = {**os.environ, 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']}

                def git(*args):
                    return subprocess.check_output(
                        ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', *args],
                        cwd=source,
                        env=environment,
                        text=True,
                    ).strip()

                git('init', '-q')
                tools = source / 'scripts/fork'
                tools.mkdir(parents=True)
                (tools / 'release_ci.py').write_text('raise RuntimeError("old application admission")\n')
                (tools / 'release_archive.py').write_text('REVISION = "old"\n')
                git('add', '.')
                git('commit', '-qm', 'application')
                application = git('rev-parse', 'HEAD')
                (tools / 'release_ci.py').write_text('from release_archive import REVISION\nprint(REVISION)\n')
                (tools / 'release_archive.py').write_text('REVISION = "workflow"\n')
                git('commit', '-qam', 'controller')
                workflow_revision = git('rev-parse', 'HEAD')
                git('checkout', '-q', application)
                workflow = yaml.safe_load((ROOT / f'.github/workflows/fork-cd-{target}.yml').read_text())
                step = next(
                    row
                    for row in workflow['jobs']['deploy']['steps']
                    if row.get('name') == 'Load release admission from workflow revision'
                )
                for revision, succeeds in [(workflow_revision, True), ('f' * 40, False)]:
                    temporary = root / revision
                    temporary.mkdir()
                    result = subprocess.run(
                        ['bash', '-e', '-o', 'pipefail', '-c', step['run']],
                        cwd=source,
                        env={**environment, 'RUNNER_TEMP': str(temporary), 'GITHUB_WORKFLOW_SHA': revision},
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                    self.assertEqual(result.stdout.strip(), 'workflow' if succeeds else '')
                self.assertEqual(git('rev-parse', 'HEAD'), application)
                self.assertEqual(git('status', '--porcelain'), '')


class DeliveryDownloadTests(unittest.TestCase):
    """Run 34362300799 reported a successful but incomplete artifact transfer."""

    def run_step(self, target, incomplete=False):
        workflow = yaml.safe_load((ROOT / f'.github/workflows/fork-cd-{target}.yml').read_text())
        step = next(
            row for row in workflow['jobs']['deploy']['steps'] if row.get('name') == 'Download complete frozen delivery'
        )
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            destination = directory / 'accepted'
            record = directory / 'attempts'
            transport = directory / 'transport.py'
            transport.write_text('''import os, sys
from pathlib import Path
args = sys.argv[1:]
assert args[:6] == ['run', 'download', '123', '--repo', 'summersmile1984/omi', '--name']
assert args[6:8] == ['delivery-synthetic-eddy-beta', '--dir']
record = Path(os.environ['DOWNLOAD_TEST_RECORD'])
attempt = int(record.read_text()) + 1 if record.exists() else 1
record.write_text(str(attempt))
destination = Path(args[8])
destination.mkdir(parents=True)
(destination / 'cloudflare.tar.gz').write_text('partial' if attempt == 1 else 'complete')
if os.environ['DOWNLOAD_TEST_INCOMPLETE'] == 'true':
    raise SystemExit(0)
if attempt == 1:
    raise SystemExit(1)
for name in ['delivery.json', 'server-images.tar', 'source.tar.gz']:
    (destination / name).write_text('complete')
''')
            launcher = directory / 'gh'
            launcher.write_text(
                '#!/bin/bash\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(transport)) + ' "$@"\n'
            )
            launcher.chmod(0o700)
            result = subprocess.run(
                ['bash', '-e', '-o', 'pipefail', '-c', step['run']],
                cwd=directory,
                env={
                    **os.environ,
                    'PATH': str(directory) + os.pathsep + os.environ['PATH'],
                    'RUNNER_TEMP': str(directory),
                    'DELIVERY_RUN_ID': '123',
                    'DELIVERY_ARTIFACT': 'delivery-synthetic-eddy-beta',
                    'DELIVERY_DIRECTORY': str(destination),
                    'DOWNLOAD_TEST_RECORD': str(record),
                    'DOWNLOAD_TEST_INCOMPLETE': str(incomplete).lower(),
                },
                capture_output=True,
                text=True,
                timeout=30,
            )
            files = {p.name: p.read_text() for p in destination.glob('*')} if destination.exists() else None
            self.assertEqual(list(directory.glob('eddy-delivery.*')), [], 'attempt-owned temporary files leaked')
            return result, int(record.read_text()), files

    def test_both_targets_retry_interrupted_transfer_without_publishing_partial_files(self):
        for target in ['cloudflare', 'server']:
            with self.subTest(target=target):
                result, attempts, files = self.run_step(target)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(attempts, 2)
                self.assertEqual(
                    files,
                    {
                        name: 'complete'
                        for name in ['delivery.json', 'cloudflare.tar.gz', 'server-images.tar', 'source.tar.gz']
                    },
                )

    def test_both_targets_refuse_false_success_after_three_incomplete_attempts(self):
        for target in ['cloudflare', 'server']:
            with self.subTest(target=target):
                result, attempts, files = self.run_step(target, incomplete=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(attempts, 3)
                self.assertIsNone(files)
                self.assertIn('deployment has not started', result.stderr)


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
