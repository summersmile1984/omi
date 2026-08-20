#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'self-host' / 'volume-snapshot.py'
OPERATIONS = SCRIPT.with_name('operations.sh')
CUTOVER_GATE = SCRIPT.with_name('cutover-https-gate.sh')
EVIDENCE_SCRIPT = SCRIPT.with_name('acceptance_evidence.py')
SPEC = importlib.util.spec_from_file_location('self_host_volume_snapshot', SCRIPT)
assert SPEC and SPEC.loader
SNAPSHOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SNAPSHOT)
EVIDENCE_SPEC = importlib.util.spec_from_file_location('self_host_acceptance_evidence', EVIDENCE_SCRIPT)
assert EVIDENCE_SPEC and EVIDENCE_SPEC.loader
EVIDENCE = importlib.util.module_from_spec(EVIDENCE_SPEC)
EVIDENCE_SPEC.loader.exec_module(EVIDENCE)
CLEAN_SOURCE_ATTRIBUTION = {
    'git_commit': 'd' * 40,
    'git_tree': 'e' * 40,
    'worktree_clean': True,
}


class SelfHostOperationsTest(unittest.TestCase):
    def test_source_attribution_rejects_dirty_cutover_without_mutating_real_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            subprocess.run(['git', 'init', '--quiet', str(repository)], check=True)
            subprocess.run(['git', '-C', str(repository), 'config', 'user.name', 'Acceptance Test'], check=True)
            subprocess.run(
                ['git', '-C', str(repository), 'config', 'user.email', 'acceptance@example.invalid'], check=True
            )
            tracked = repository / 'tracked.txt'
            tracked.write_text('version one\n', encoding='utf-8')
            subprocess.run(['git', '-C', str(repository), 'add', 'tracked.txt'], check=True)
            subprocess.run(['git', '-C', str(repository), 'commit', '--quiet', '-m', 'fixture'], check=True)
            real_index = repository / '.git' / 'index'
            index_before = real_index.read_bytes()

            clean_cli = subprocess.run(
                [
                    sys.executable,
                    str(EVIDENCE_SCRIPT),
                    '--source-attribution',
                    '--root',
                    str(repository),
                    '--require-clean',
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(clean_cli.returncode, 0, clean_cli.stderr)
            clean = json.loads(clean_cli.stdout)
            self.assertTrue(clean['worktree_clean'])
            self.assertEqual(
                clean['git_tree'],
                subprocess.run(
                    ['git', '-C', str(repository), 'rev-parse', 'HEAD^{tree}'],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
            )

            tracked.write_text('version two\n', encoding='utf-8')
            (repository / 'untracked.txt').write_text('new content\n', encoding='utf-8')
            dirty_cli = subprocess.run(
                [
                    sys.executable,
                    str(EVIDENCE_SCRIPT),
                    '--source-attribution',
                    '--root',
                    str(repository),
                    '--require-clean',
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(dirty_cli.returncode, 1)
            self.assertIn('requires a clean worktree', dirty_cli.stderr)

            dirty = EVIDENCE.resolve_source_attribution(repository, require_clean=False)
            self.assertFalse(dirty['worktree_clean'])
            self.assertNotEqual(dirty['git_tree'], clean['git_tree'])
            self.assertEqual(real_index.read_bytes(), index_before)
            staged = subprocess.run(
                ['git', '-C', str(repository), 'diff', '--cached', '--quiet'],
                check=False,
            )
            self.assertEqual(staged.returncode, 0)

    def test_local_cutover_uses_internal_tls_listener_not_published_host_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            searxng_secret = 'acceptance-test-secret'
            searxng_secret_sha256 = hashlib.sha256(searxng_secret.encode()).hexdigest()
            env_file = root / 'production.env'
            env_file.write_text(
                '\n'.join(
                    (
                        'BETTER_AUTH_TRUSTED_ORIGINS=https://app.omi.test',
                        'PUBLIC_BACKEND_URL=https://api.omi.test',
                        'PUBLIC_AUTH_URL=https://auth.omi.test',
                        'PUBLIC_MCP_URL=https://mcp.omi.test',
                        f'SEARXNG_SECRET={searxng_secret}',
                    )
                )
                + '\n',
                encoding='utf-8',
            )
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            call_log = root / 'docker.calls'
            docker = bin_dir / 'docker'
            docker.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_DOCKER_CALLS"\n'
                'case "$*" in *"exec -T searxng"*) '
                f'printf \'%s\\n\' \'{{"effective_secret_nonempty":true,"effective_secret_not_known_default":true,"effective_secret_sha256":"{searxng_secret_sha256}"}}\';; esac\n'
                'exit 0\n',
                encoding='utf-8',
            )
            docker.chmod(0o755)
            environment = {
                **os.environ,
                'PATH': f'{bin_dir}:{os.environ["PATH"]}',
                'SELF_HOST_ENV': str(env_file),
                'FAKE_DOCKER_CALLS': str(call_log),
                'CUTOVER_HTTPS_PORT': '18443',
            }
            accepted = subprocess.run(
                ['bash', str(CUTOVER_GATE), '--local'],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            calls = call_log.read_text(encoding='utf-8')
            self.assertIn('--env PUBLIC_AUTH_URL=https://auth.omi.test', calls)
            self.assertNotIn('PUBLIC_AUTH_URL=https://auth.omi.test:18443', calls)

            env_file.write_text(
                env_file.read_text(encoding='utf-8').replace(
                    f'SEARXNG_SECRET={searxng_secret}',
                    'SEARXNG_SECRET=a-different-reviewed-secret',
                ),
                encoding='utf-8',
            )
            mismatched_secret = subprocess.run(
                ['bash', str(CUTOVER_GATE), '--local'],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(mismatched_secret.returncode, 1)
            self.assertIn('did not apply a non-default runtime secret', mismatched_secret.stderr)

            env_file.write_text(
                env_file.read_text(encoding='utf-8').replace(
                    'PUBLIC_AUTH_URL=https://auth.omi.test',
                    'PUBLIC_AUTH_URL=https://auth.omi.test:18443',
                ),
                encoding='utf-8',
            )
            rejected = subprocess.run(
                ['bash', str(CUTOVER_GATE), '--local'],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn('origins without a port', rejected.stderr)

    def test_cutover_evidence_requires_external_edge_and_live_socket_denial(self) -> None:
        assembled = {
            'status': 'passed',
            'assembled_product_loop': {
                'capture': {'fixture_manifest_match': True},
                'remember': {'long_term_admission': 'passed'},
            },
            'live_egress': {
                'enforcement': 'not_enforced_by_compose',
                'sentinel_targets_denied': [],
                'workloads': [],
                'operator_policy_artifact_sha256': None,
            },
        }
        local = EVIDENCE.build_evidence(
            mode='cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=assembled,
            checked_at='2026-08-20T00:00:00+00:00',
        )
        self.assertTrue(local['authorizes_tested_configuration_cutover'])
        self.assertFalse(local['authorizes_production_cutover'])
        self.assertEqual(local['remaining_cutover_reason'], 'intended_public_dns_certificate_and_edge_not_exercised')
        self.assertEqual(local['gates']['live_sentinel_egress_policy']['enforcement'], 'not_enforced_by_compose')
        self.assertEqual(local['gates']['hermetic_undeclared_dns_and_socket_egress'], 'denied')
        self.assertFalse(local['gates']['live_dns_denial_claimed'])

        external_without_policy = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=assembled,
            checked_at='2026-08-20T00:00:00+00:00',
        )
        self.assertFalse(external_without_policy['authorizes_production_cutover'])
        self.assertEqual(
            external_without_policy['remaining_cutover_reason'],
            'live_sentinel_egress_or_operator_policy_evidence_missing',
        )

        assembled['live_egress'] = {
            'enforcement': 'sentinel_targets_denied_with_operator_policy',
            'sentinel_targets_denied': [
                'api.openai.com',
                'generativelanguage.googleapis.com',
                'api.anthropic.com',
                'api.omi.me',
                '1.1.1.1',
            ],
            'workloads': ['backend', 'queue-worker', 'auth-server'],
            'operator_policy_artifact_sha256': 'a' * 64,
            'scope': 'sentinel_targets_only',
        }
        external_with_policy = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=assembled,
            checked_at='2026-08-20T00:00:00+00:00',
        )
        self.assertTrue(external_with_policy['authorizes_production_cutover'])
        self.assertIsNone(external_with_policy['remaining_cutover_reason'])

        dirty_source = {**CLEAN_SOURCE_ATTRIBUTION, 'git_tree': 'f' * 40, 'worktree_clean': False}
        dirty_external = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=dirty_source,
            live_replacement={'status': 'passed'},
            assembled_loop=assembled,
            checked_at='2026-08-20T00:00:00+00:00',
        )
        self.assertFalse(dirty_external['authorizes_tested_configuration_cutover'])
        self.assertFalse(dirty_external['authorizes_production_cutover'])
        self.assertEqual(dirty_external['remaining_cutover_reason'], 'source_worktree_not_clean')

        for replacement in (None, {'status': 'failed'}):
            external_without_replacements = EVIDENCE.build_evidence(
                mode='external-cutover-live',
                source_attribution=CLEAN_SOURCE_ATTRIBUTION,
                live_replacement=replacement,
                assembled_loop=assembled,
                checked_at='2026-08-20T00:00:00+00:00',
            )
            self.assertFalse(external_without_replacements['authorizes_tested_configuration_cutover'])
            self.assertFalse(external_without_replacements['authorizes_production_cutover'])
            self.assertEqual(
                external_without_replacements['remaining_cutover_reason'],
                'live_replacement_services_not_passed',
            )

            local_without_replacements = EVIDENCE.build_evidence(
                mode='cutover-live',
                source_attribution=CLEAN_SOURCE_ATTRIBUTION,
                live_replacement=replacement,
                assembled_loop=assembled,
                checked_at='2026-08-20T00:00:00+00:00',
            )
            self.assertFalse(local_without_replacements['authorizes_tested_configuration_cutover'])
            self.assertEqual(
                local_without_replacements['remaining_cutover_reason'],
                'live_replacement_services_not_passed',
            )

        assembled['assembled_product_loop']['remember']['long_term_admission'] = 'retry_pending'
        external_without_long_term_admission = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=assembled,
            checked_at='2026-08-20T00:00:00+00:00',
        )
        self.assertFalse(external_without_long_term_admission['authorizes_tested_configuration_cutover'])
        self.assertFalse(external_without_long_term_admission['authorizes_production_cutover'])
        self.assertEqual(
            external_without_long_term_admission['remaining_cutover_reason'],
            'canonical_long_term_admission_not_passed',
        )

        non_cutover_mode = EVIDENCE.build_evidence(
            mode='live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop={
                **assembled,
                'assembled_product_loop': {
                    **assembled['assembled_product_loop'],
                    'remember': {'long_term_admission': 'passed'},
                },
            },
            checked_at='2026-08-20T00:00:00+00:00',
        )
        self.assertFalse(non_cutover_mode['authorizes_tested_configuration_cutover'])
        self.assertFalse(non_cutover_mode['authorizes_production_cutover'])

    def test_external_cutover_requires_policy_artifact_and_probes_all_sentinels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            searxng_secret = 'acceptance-test-secret'
            searxng_secret_sha256 = hashlib.sha256(searxng_secret.encode()).hexdigest()
            env_file = root / 'production.env'
            env_file.write_text(
                '\n'.join(
                    (
                        'BETTER_AUTH_TRUSTED_ORIGINS=https://app.example.org',
                        'PUBLIC_BACKEND_URL=https://api.example.org',
                        'PUBLIC_AUTH_URL=https://auth.example.org',
                        'PUBLIC_MCP_URL=https://mcp.example.org',
                        f'SEARXNG_SECRET={searxng_secret}',
                    )
                )
                + '\n',
                encoding='utf-8',
            )
            policy = root / 'egress-policy.yaml'
            policy.write_text('policy: deny application public egress\n', encoding='utf-8')
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            call_log = root / 'docker.calls'
            docker = bin_dir / 'docker'
            docker.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_DOCKER_CALLS"\n'
                'case "$*" in *"ps --format json backend queue-worker auth-server"*) '
                'printf \'%s\\n\' \'{"Service":"backend","State":"running","Health":"healthy"}\' '
                '\'{"Service":"queue-worker","State":"running","Health":"healthy"}\' '
                '\'{"Service":"auth-server","State":"running","Health":"healthy"}\';; esac\n'
                'case "$*" in *"exec -T searxng"*) '
                f'printf \'%s\\n\' \'{{"effective_secret_nonempty":true,"effective_secret_not_known_default":true,"effective_secret_sha256":"{searxng_secret_sha256}"}}\';; esac\n'
                'exit 0\n',
                encoding='utf-8',
            )
            docker.chmod(0o755)
            base_environment = {
                **os.environ,
                'PATH': f'{bin_dir}:{os.environ["PATH"]}',
                'SELF_HOST_ENV': str(env_file),
                'FAKE_DOCKER_CALLS': str(call_log),
            }
            base_environment.pop('SELF_HOST_EGRESS_POLICY_ARTIFACT', None)
            missing_policy = subprocess.run(
                ['bash', str(CUTOVER_GATE), '--external'],
                check=False,
                capture_output=True,
                text=True,
                env=base_environment,
            )
            self.assertNotEqual(missing_policy.returncode, 0, missing_policy.stderr + missing_policy.stdout)
            self.assertIn('SELF_HOST_EGRESS_POLICY_ARTIFACT', missing_policy.stderr)

            accepted = subprocess.run(
                ['bash', str(CUTOVER_GATE), '--external'],
                check=False,
                capture_output=True,
                text=True,
                env={**base_environment, 'SELF_HOST_EGRESS_POLICY_ARTIFACT': str(policy)},
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            calls = call_log.read_text(encoding='utf-8')
            for service in ('backend', 'queue-worker', 'auth-server'):
                self.assertIn(f'exec -T {service}', calls)
            self.assertNotIn('run --rm --no-deps -T backend', calls)
            for target in (
                'api.openai.com',
                'generativelanguage.googleapis.com',
                'api.anthropic.com',
                'api.omi.me',
                '1.1.1.1',
            ):
                self.assertIn(target, calls)

            docker.write_text(
                docker.read_text(encoding='utf-8').replace(
                    '{"Service":"backend","State":"running","Health":"healthy"}',
                    '{"Service":"backend","State":"running","Health":"starting"}',
                ),
                encoding='utf-8',
            )
            unhealthy = subprocess.run(
                ['bash', str(CUTOVER_GATE), '--external'],
                check=False,
                capture_output=True,
                text=True,
                env={**base_environment, 'SELF_HOST_EGRESS_POLICY_ARTIFACT': str(policy)},
            )
            self.assertEqual(unhealthy.returncode, 1)
            self.assertIn('must be the running healthy workloads', unhealthy.stderr)

    def test_operations_entrypoint_self_check(self) -> None:
        result = subprocess.run(
            ['bash', str(OPERATIONS), 'self-check'],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('self-host operations self-check OK', result.stdout)

    def test_volume_backup_restore_and_manifest_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'state'
            source.mkdir()
            (source / 'nested').mkdir()
            (source / 'nested' / 'record.json').write_text('{"version":1}\n', encoding='utf-8')
            archive = root / 'state.tar.gz'

            SNAPSHOT.backup(source, archive)
            SNAPSHOT.write_manifest(root, 'deadbeef', [archive.name])
            SNAPSHOT.verify_manifest(root)

            (source / 'nested' / 'record.json').write_text('corrupt', encoding='utf-8')
            (source / 'stale').write_text('must disappear', encoding='utf-8')
            SNAPSHOT.restore(source, archive)
            self.assertEqual((source / 'nested' / 'record.json').read_text(encoding='utf-8'), '{"version":1}\n')
            self.assertFalse((source / 'stale').exists())

            archive.write_bytes(archive.read_bytes() + b'tampered')
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                SNAPSHOT.verify_manifest(root)

    def test_restore_rejects_archive_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'state'
            source.mkdir()
            archive_path = root / 'unsafe.tar.gz'
            with tarfile.open(archive_path, 'w:gz') as archive:
                member = tarfile.TarInfo('../outside')
                member.size = 1
                archive.addfile(member, io.BytesIO(b'x'))

            with self.assertRaisesRegex(RuntimeError, 'unsafe archive member'):
                SNAPSHOT.restore(source, archive_path)

    def test_manifest_records_source_revision_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / 'postgres.dump'
            artifact.write_bytes(b'dump')
            SNAPSHOT.write_manifest(root, 'cafebabe', [artifact.name])

            payload = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(payload['git_sha'], 'cafebabe')
            self.assertEqual(set(payload['artifacts']), {'postgres.dump'})
            self.assertNotIn('secret', json.dumps(payload).lower())

    def test_restore_and_start_static_contract_is_fail_closed(self) -> None:
        """Tripwire for the destructive ordering; live Compose proves behavior."""

        script = OPERATIONS.read_text(encoding='utf-8')
        recreate = script.index('dropdb -U "$POSTGRES_USER" --force --if-exists')
        restore = script.index("pg_restore -U \"$POSTGRES_USER\"")
        migration = script.index('compose run --rm --no-deps -T auth-migrate')
        application_start = script.index('compose up --detach --wait --no-deps "${APPLICATION_SERVICES[@]}"')
        self.assertLess(recreate, restore)
        self.assertLess(migration, application_start)


if __name__ == '__main__':
    unittest.main()
