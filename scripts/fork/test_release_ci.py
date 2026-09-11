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

from release_ci import (
    ATTESTATION_MANIFEST,
    CI_PATH,
    PREPARE_PATH,
    RELEASE_JOBS,
    REPOSITORY,
    expected_ci_checks,
    list_attestations,
    manifest_path,
    resolve_delivery,
    sha256_file,
    verify_ci,
    verify_delivery,
)
from release_archive import pack_candidate, unpack_candidate
from download_delivery import download, FILES as DELIVERY_FILES, ATTEMPTS
from workflow_lint import resolve_constant_runners
import zipfile

ROOT = Path(__file__).resolve().parents[2]



def load_module_release_ci():
    import importlib.util

    spec = importlib.util.spec_from_file_location('release_ci', Path(__file__).resolve().parent / 'release_ci.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkflowRunnerLabelsTests(unittest.TestCase):
    def test_fork_runner_labels_are_still_checked_by_the_actual_linter(self):
        for label in ['mac-studio', 'misspelled-fork-runner']:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                workflow = Path(directory) / 'fixture.yml'
                selector = json.dumps(['self-hosted', 'macOS', 'ARM64', label, 'memweft'])
                workflow.write_text(
                    'on: workflow_dispatch\njobs:\n  fixture:\n'
                    "    runs-on: ${{ fromJSON('" + selector + "') }}\n"
                    '    steps:\n      - run: exit 0\n'
                )
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'scripts/fork/workflow_lint.py'), str(workflow)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode == 0, label == 'mac-studio', result.stdout + result.stderr)
                if label != 'mac-studio':
                    self.assertIn('runner-label', result.stdout + result.stderr)

    def test_selector_resolution_preserves_workflow_lines_and_rejects_wrong_shapes(self):
        prefix = 'name: fixture\njobs:\n  fixture:\n    runs-on: '
        suffix = "    steps:\n      - run: |\n          runs-on: ${{ fromJSON('[1]') }}\n"
        source = prefix + "${{ fromJSON('[\"self-hosted\",\"memweft\"]') }}\n" + suffix
        self.assertEqual(resolve_constant_runners(source), prefix + '["self-hosted", "memweft"]\n' + suffix)
        for value in ['{}', 'null', '[]', '[1]']:
            with self.assertRaises(ValueError):
                resolve_constant_runners(prefix + "${{ fromJSON('" + value + "') }}")


class CloudflareContinuationHandoffTests(unittest.TestCase):
    """Run the actual wrapper; only the downstream remote publisher is controlled."""

    def run_wrapper(self, previous):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            delivery = directory / 'delivery'
            candidate_dir = delivery / 'unpacked/cloudflare'
            candidate_dir.mkdir(parents=True)
            candidate = {
                'candidate_digest': 'c' * 64,
                'source': {'commit': 'a' * 40},
                'inventory': {'secret_refs': {}},
            }
            (candidate_dir / 'candidate.json').write_text(json.dumps(candidate))
            (delivery / 'delivery.json').write_text(
                json.dumps(
                    {
                        'candidate_digest': 'c' * 64,
                        'commit': 'a' * 40,
                        'stage': 'beta',
                        'ci_run_id': 123,
                        'cloudflare_continue_from': previous,
                    }
                )
            )
            publisher = directory / 'deploy/cloudflare/scripts/release.mjs'
            publisher.parent.mkdir(parents=True)
            publisher.write_text(
                'import {writeFileSync} from "node:fs";'
                'writeFileSync(process.env.CAPTURE, JSON.stringify(process.argv.slice(2)));'
            )
            capture = directory / 'capture.json'
            command = [
                shutil.which('node'),
                str(ROOT / 'scripts/fork/deploy-cloudflare.mjs'),
                '--delivery',
                str(delivery),
                '--journal-root',
                str(directory / 'journals'),
            ]
            result = subprocess.run(
                command,
                cwd=directory,
                text=True,
                capture_output=True,
                env={'PATH': os.environ['PATH'], 'CAPTURE': str(capture), 'CLOUDFLARE_API_TOKEN': 'synthetic-fixture'},
            )
            args = json.loads(capture.read_text()) if capture.exists() else None
            if args:
                retained = Path(args[args.index('--candidate') + 1])
                self.assertEqual(json.loads((retained / 'candidate.json').read_text()), candidate)
            return result, args

    def test_new_and_continued_releases_retain_candidate_and_forward_exact_journal(self):
        previous = 'beta-' + 'a' * 40 + '-11111111-1111-4111-8111-111111111111'
        for value in ['', previous]:
            with self.subTest(previous=value):
                result, args = self.run_wrapper(value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(args[0], 'apply')
                self.assertEqual(args[args.index('--authorize') + 1], 'c' * 64)
                if value:
                    self.assertEqual(Path(args[args.index('--continue-from') + 1]).name, value)
                else:
                    self.assertNotIn('--continue-from', args)

    def test_cross_stage_and_external_journals_never_reach_the_publisher(self):
        for value in ['../other-journal', '/absolute/journal', 'production-' + 'a' * 40 + '-' + '1' * 36]:
            with self.subTest(previous=value):
                result, args = self.run_wrapper(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(args)
                self.assertIn('continuation must name one retained journal in this stage', result.stderr)


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
                (tools / 'download_delivery.py').write_text('VERSION = "old"\n')
                git('add', '.')
                git('commit', '-qm', 'application')
                application = git('rev-parse', 'HEAD')
                (tools / 'release_ci.py').write_text('from release_archive import REVISION\nprint(REVISION)\n')
                (tools / 'release_archive.py').write_text('REVISION = "workflow"\n')
                (tools / 'download_delivery.py').write_text('VERSION = "workflow"\n')
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
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for name in sorted(DELIVERY_FILES):
                archive.writestr(name, 'frozen ' + name)
        self.payload = buffer.getvalue()
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.artifact = dict(
            id=123, name='selected', expired=False, digest='sha256:' + self.digest, size_in_bytes=len(self.payload)
        )
        self.cache = self.root / 'cache'
        self.partial = self.cache / '123' / self.digest / 'artifact.zip.partial'
        self.metadata_reads = 0

    def api(self, path):
        self.assertEqual(path, 'actions/runs/456/artifacts?per_page=100')
        self.metadata_reads += 1
        return {'artifacts': [self.artifact]}

    def execute(self, destination, fetch):
        return download(
            456,
            'selected',
            destination,
            self.cache,
            api=self.api,
            locate=lambda value: f'https://artifact.example/{value}',
            fetch=fetch,
        )

    def test_first_download_without_any_cache_verifies_and_extracts(self):
        def fetch(url, partial):
            self.assertFalse(partial.exists())
            partial.write_bytes(self.payload)
            return True

        self.execute(self.root / 'first', fetch)
        self.assertEqual({path.name for path in (self.root / 'first').iterdir()}, DELIVERY_FILES)
        self.assertFalse(self.partial.exists())

    def test_resume_preserves_received_bytes_and_cache_reuse_rechecks_github_and_zip_hash(self):
        self.partial.parent.mkdir(parents=True)
        self.partial.write_bytes(self.payload[:100])
        offsets = []

        def fetch(url, partial):
            self.assertEqual(url, 'https://artifact.example/123')
            offset = partial.stat().st_size
            offsets.append(offset)
            stop = len(self.payload) // 2 if len(offsets) == 1 else len(self.payload)
            with partial.open('ab') as output:
                output.write(self.payload[offset:stop])
            return stop == len(self.payload)

        self.execute(self.root / 'first', fetch)
        self.assertEqual(offsets, [100, len(self.payload) // 2])
        self.assertEqual({p.name for p in (self.root / 'first').iterdir()}, DELIVERY_FILES)
        self.execute(self.root / 'second', lambda *_: self.fail('verified cache unexpectedly downloaded again'))
        self.assertEqual(self.metadata_reads, 2)
        archive = self.partial.with_name('artifact.zip')
        archive.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'cached artifact ZIP'):
            self.execute(self.root / 'third', lambda *_: self.fail('corrupt cache was silently replaced'))
        self.assertFalse((self.root / 'third').exists())

    def test_false_success_on_incomplete_transfer_never_enters_admission(self):
        calls = []

        def fetch(url, partial):
            calls.append(url)
            with partial.open('ab') as output:
                output.write(b'x')
            return True

        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            self.execute(self.root / 'output', fetch)
        self.assertEqual(len(calls), ATTEMPTS)
        self.assertFalse((self.root / 'output').exists())
        self.assertEqual(self.partial.stat().st_size, ATTEMPTS)
        self.assertFalse(self.partial.with_name('artifact.zip').exists())

    def test_full_size_without_the_github_digest_is_not_a_verified_cache(self):
        def fetch(url, partial):
            partial.write_bytes(b'x' * len(self.payload))
            return True

        with self.assertRaisesRegex(ValueError, 'ZIP hash differs'):
            self.execute(self.root / 'output', fetch)
        self.assertFalse((self.root / 'output').exists())
        self.assertFalse(self.partial.with_name('artifact.zip').exists())


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


class ReusableDeliveryWorkflowTests(unittest.TestCase):
    def workflow(self, target):
        return yaml.safe_load((ROOT / f'.github/workflows/fork-cd-{target}.yml').read_text())

    def test_cloudflare_call_declares_secret_inheritance_static_contract(self):
        # Static wiring guard for run 34401734852: its environment existed but
        # both credentials resolved empty across the undeclared call boundary.
        workflow = yaml.safe_load((ROOT / PREPARE_PATH).read_text())
        self.assertEqual(workflow['jobs']['cloudflare']['secrets'], 'inherit')
        self.assertEqual(self.workflow('cloudflare')['jobs']['deploy']['environment'], 'cloudflare-${{ inputs.stage }}')

    def test_cloudflare_credentials_fail_before_setup_and_never_print_values(self):
        step = self.workflow('cloudflare')['jobs']['deploy']['steps'][0]
        for missing in [None, 'CLOUDFLARE_API_TOKEN', 'RELEASE_SECRETS_JSON']:
            with self.subTest(missing=missing):
                credentials = {
                    'CLOUDFLARE_API_TOKEN': 'synthetic-cloud-credential',
                    'RELEASE_SECRETS_JSON': '{"SYNTHETIC":"private-fixture-value"}',
                }
                if missing:
                    credentials[missing] = ''
                result = subprocess.run(
                    ['bash', '-e', '-o', 'pipefail', '-c', step['run']],
                    env={**os.environ, **credentials},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 1 if missing else 0, result.stderr)
                output = result.stdout + result.stderr
                self.assertNotIn('synthetic-cloud-credential', output)
                self.assertNotIn('private-fixture-value', output)
                if missing:
                    self.assertIn('reusable-workflow secret inheritance', output)

    def execute(self, step, environment, *, fail_verify=False):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            capture = directory / 'commands.jsonl'
            fake = directory / 'capture.py'
            fake.write_text(
                'import json, os, sys\n'
                'with open(os.environ["CAPTURE"], "a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                'if os.environ.get("FAIL_VERIFY") == "true" and "verify" in sys.argv: sys.exit(47)\n'
            )
            for name in ('python3', 'node'):
                launcher = directory / name
                launcher.write_text(
                    '#!/bin/bash\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(fake)) + ' "$@"\n'
                )
                launcher.chmod(0o700)
            output = directory / 'outputs'
            result = subprocess.run(
                ['bash', '-e', '-o', 'pipefail', '-c', step['run']],
                env={
                    **os.environ,
                    'PATH': str(directory) + os.pathsep + os.environ['PATH'],
                    'CAPTURE': str(capture),
                    'FAIL_VERIFY': str(fail_verify).lower(),
                    'GITHUB_OUTPUT': str(output),
                    'RUNNER_TEMP': str(directory),
                    **environment,
                },
                capture_output=True,
                text=True,
            )
            commands = [json.loads(line) for line in capture.read_text().splitlines()] if capture.exists() else []
            return result, commands, output.read_text() if output.exists() else ''

    def test_private_qualification_selects_only_the_current_run_artifact_and_cd_keeps_full_admission(self):
        sha = 'a' * 40
        for target in ('cloudflare', 'server'):
            step = self.workflow(target)['jobs']['resolve']['steps'][-1]
            env = {
                'GITHUB_SHA': sha,
                'GITHUB_RUN_ID': '12',
                'DELIVERY_RUN_ID': '12',
                'RELEASE_STAGE': 'beta',
                'QUALIFICATION_ARTIFACT': f'delivery-{sha}-eddy-beta',
            }
            result, commands, output = self.execute(step, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(commands, [])
            self.assertEqual(output, f'sha={sha}\nartifact=delivery-{sha}-eddy-beta\n')
            for changed in [{'DELIVERY_RUN_ID': '11'}, {'QUALIFICATION_ARTIFACT': f'delivery-{sha}-eddy-production'}]:
                result, commands, _ = self.execute(step, {**env, **changed})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(commands, [])
            result, commands, _ = self.execute(step, {**env, 'QUALIFICATION_ARTIFACT': ''})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(commands, [['scripts/fork/release_ci.py', 'resolve', '--run-id', '12', '--stage', 'beta']])

    def test_shared_execution_verifies_before_selecting_private_qualification_or_persistent_deploy(self):
        for target in ('cloudflare', 'server'):
            # Select by name, not by position: the deploy job now ends with the
            # failure-evidence steps, which do not run on a successful release.
            step = next(
                entry
                for entry in self.workflow(target)['jobs']['deploy']['steps']
                if entry.get('name', '').startswith('Verify and execute')
            )
            for qualification in (True, False):
                for failure in (True, False):
                    env = {
                        'RELEASE_DIRECTORY': '/fixture/delivery',
                        'RELEASE_SHA': 'a' * 40,
                        'RELEASE_STAGE': 'beta',
                        'QUALIFICATION_ARTIFACT': 'fixture' if qualification else '',
                        'RELEASE_JOURNAL_ROOT': '/fixture/journals',
                        'SERVER_DEPLOY_ROOT': '/fixture/server',
                        'SERVER_DOCKER_CONTEXT': 'colima-eddy-server',
                    }
                    result, commands, _ = self.execute(step, env, fail_verify=failure)
                    self.assertEqual(result.returncode, 47 if failure else 0, result.stderr)
                    self.assertIn('verify', commands[0])
                    self.assertEqual(len(commands), 1 if failure else 2)
                    if not failure and target == 'cloudflare':
                        self.assertEqual(
                            commands[1][0],
                            (
                                'deploy/cloudflare/scripts/release-cloud-probe.mjs'
                                if qualification
                                else 'scripts/fork/deploy-cloudflare.mjs'
                            ),
                        )
                    elif not failure:
                        self.assertEqual(
                            commands[1][:2], ['scripts/fork/deploy_server.py', 'qualify' if qualification else 'deploy']
                        )


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

    def attestation_payloads(self, *, run_id=1, attempt=1, sha=None, check_ids=None, manifest_sha256=None):
        """Two jobs' worth of manifest attestations, split like the real lanes."""
        sha = sha or self.sha
        identifiers = sorted(expected_ci_checks(ATTESTATION_MANIFEST) if check_ids is None else check_ids)
        half = (len(identifiers) + 1) // 2
        groups = [group for group in (identifiers[:half], identifiers[half:]) if group]
        if not groups:
            groups = [[]]
        return [
            {
                'schema_version': 1,
                'lane': 'ci',
                'platform': platform,
                'check_ids': group,
                'manifest_sha256': manifest_sha256 or sha256_file(manifest_path(ATTESTATION_MANIFEST)),
                'run_id': run_id,
                'run_attempt': attempt,
                'sha': sha,
            }
            for platform, group in zip(('linux', 'macos'), groups)
        ]

    def reader(self, payloads):
        return lambda run_id, attempt, sha, api: payloads

    def api(self, path):
        if '/jobs?' in path:
            return {'jobs': self.jobs}
        if path.startswith('compare/'):
            return {'status': 'ahead'}
        if '/artifacts?' in path:
            return {'artifacts': [self.artifact]}
        return self.run

    def test_full_same_source_ci_passes(self):
        self.assertEqual(
            verify_ci(1, self.sha, self.api, self.reader(self.attestation_payloads()))['commit'], self.sha
        )

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
                    verify_ci(1, self.sha, self.api, self.reader(self.attestation_payloads()))
                self.run[field] = original
        self.jobs[1]['conclusion'] = 'skipped'
        with self.assertRaises(ValueError):
            verify_ci(1, self.sha, self.api, self.reader(self.attestation_payloads()))

    def test_a_run_that_did_not_execute_the_whole_manifest_is_refused(self):
        # Job names alone cannot prove coverage: the same two jobs also serve the
        # diff-scoped push and pull-request lanes.
        complete = sorted(expected_ci_checks(ATTESTATION_MANIFEST))
        cases = {
            'a check is missing': (complete[:-1], None),
            'no attestation was published': ([], None),
            'the manifest digest differs': (complete, 'f' * 64),
        }
        for label, (identifiers, digest) in cases.items():
            with self.subTest(label=label):
                payloads = self.attestation_payloads(check_ids=identifiers, manifest_sha256=digest)
                with self.assertRaises(ValueError):
                    verify_ci(1, self.sha, self.api, self.reader(payloads))

    def test_an_attestation_from_another_run_or_lane_is_refused(self):
        for label, overrides in (
            ('another attempt', {'attempt': 2}),
            ('another source', {'sha': 'c' * 40}),
            ('another run', {'run_id': 99}),
        ):
            with self.subTest(label=label):
                payloads = self.attestation_payloads(**overrides)
                with self.assertRaises(ValueError):
                    verify_ci(1, self.sha, self.api, self.reader(payloads))
        payloads = self.attestation_payloads()
        for payload in payloads:
            payload['lane'] = 'push'
        with self.assertRaises(ValueError):
            verify_ci(1, self.sha, self.api, self.reader(payloads))
        for payload in self.attestation_payloads():
            payload['schema_version'] = 2
        with self.assertRaises(ValueError):
            verify_ci(1, self.sha, self.api, self.reader(self.attestation_payloads()[:1] + [{'schema_version': 2}]))

    def test_delivery_run_must_have_correct_stage_artifact(self):
        self.run['path'] = PREPARE_PATH
        self.jobs = [{'name': name, 'conclusion': 'success'} for name in RELEASE_JOBS]
        self.assertEqual(resolve_delivery(2, 'beta', self.api)['sha'], self.sha)
        with self.assertRaises(ValueError):
            resolve_delivery(2, 'production', self.api)

    def test_legacy_build_only_runs_and_incomplete_artifact_qualification_cannot_authorize_cd(self):
        self.run['path'] = PREPARE_PATH
        for jobs in [
            [{'name': 'prepare', 'conclusion': 'success'}],
            [
                {
                    'name': name,
                    'conclusion': (
                        'skipped'
                        if name == 'Cloudflare artifact qualification / Execute accepted delivery'
                        else 'success'
                    ),
                }
                for name in RELEASE_JOBS
            ],
            [
                {
                    'name': name,
                    'conclusion': (
                        'failure' if name == 'Server image qualification / Execute accepted delivery' else 'success'
                    ),
                }
                for name in RELEASE_JOBS
            ],
        ]:
            with self.subTest(jobs=jobs):
                self.jobs = jobs
                with self.assertRaisesRegex(ValueError, 'release CI must qualify'):
                    resolve_delivery(2, 'beta', self.api)

    def test_historical_green_release_without_runtime_and_ingress_contract_cannot_authorize_cd(self):
        # Run 34415705069 passed its old six jobs, then CD 34419433912
        # failed on the public Web readiness route and ingress policy.
        self.run['path'] = PREPARE_PATH
        self.jobs = [
            {'name': name, 'conclusion': 'success'}
            for name in [
                'Freeze delivery artifacts',
                'Cloudflare artifact qualification / Resolve delivery',
                'Cloudflare artifact qualification / Execute accepted delivery',
                'Server image qualification / Resolve delivery',
                'Server image qualification / Execute accepted delivery',
                'Release ready',
            ]
        ]
        with self.assertRaisesRegex(ValueError, 'runtime readiness and public ingress'):
            resolve_delivery(34415705069, 'beta', self.api)
        self.jobs[-1]['name'] = 'Release ready (runtime and public ingress)'
        self.assertEqual(resolve_delivery(2, 'beta', self.api)['sha'], self.sha)

    def test_release_ready_executes_the_workflow_gate_for_success_failure_and_skipped_jobs(self):
        workflow = yaml.safe_load((ROOT / PREPARE_PATH).read_text())
        names = set()
        for job in workflow['jobs'].values():
            if 'uses' in job:
                called = yaml.safe_load((ROOT / job['uses']).read_text())
                names.update(job['name'] + ' / ' + child['name'] for child in called['jobs'].values())
            else:
                names.add(job['name'])
        self.assertEqual(names, RELEASE_JOBS)
        step = workflow['jobs']['ready']['steps'][0]
        for outcome in ['success', 'failure', 'skipped', 'cancelled', '']:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    ['bash', '-e', '-o', 'pipefail', '-c', step['run']],
                    env={
                        **os.environ,
                        'PREPARE_RESULT': 'success',
                        'CLOUDFLARE_RESULT': outcome,
                        'SERVER_RESULT': 'success',
                        'GITHUB_STEP_SUMMARY': str(Path(directory) / 'summary'),
                    },
                    capture_output=True,
                )
                self.assertEqual(result.returncode == 0, outcome == 'success')

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
            verify_delivery(
                directory, self.sha, 'beta', 'self_hosted', self.api,
                self.reader(self.attestation_payloads()),
            )
            archive.write_bytes(b'different images')
            with self.assertRaises(ValueError):
                verify_delivery(
                    directory, self.sha, 'beta', 'self_hosted', self.api,
                    self.reader(self.attestation_payloads()),
                )


    def test_attestations_are_listed_from_the_run_not_an_attempt(self):
        # `/attempts/{n}/artifacts` does not exist and returns 404; the first real
        # run of this admission path failed there. The per-attempt binding comes
        # from the payload, not from the listing URL.
        module = load_module_release_ci()
        requested = []

        def api(path):
            requested.append(path)
            return {
                'artifacts': [
                    {'name': f'fork-ci-attestation-linux-{self.sha}', 'expired': False},
                    {'name': f'fork-ci-attestation-macos-{self.sha}', 'expired': True},
                    {'name': f'fork-ci-attestation-linux-{"b" * 40}', 'expired': False},
                    {'name': f'delivery-{self.sha}-eddy-beta', 'expired': False},
                ]
            }

        selected = module.list_attestations(7, self.sha, api)
        self.assertEqual(requested, ['actions/runs/7/artifacts?per_page=100'])
        self.assertEqual([item['name'] for item in selected], [f'fork-ci-attestation-linux-{self.sha}'])


if __name__ == '__main__':
    unittest.main()
