#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import os
import subprocess
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'self-host' / 'volume-snapshot.py'
OPERATIONS = SCRIPT.with_name('operations.sh')
CUTOVER_GATE = SCRIPT.with_name('cutover-https-gate.sh')
CUTOVER_OVERLAY = SCRIPT.with_name('compose.cutover-acceptance.yml')
COMPOSE_WRAPPER = SCRIPT.with_name('compose-clean-env.sh')
ZERO_VENDOR_ACCEPTANCE = SCRIPT.with_name('zero-vendor-acceptance.sh')
EGRESS_POLICY_CONTRACT = SCRIPT.with_name('egress-policy-contract.py')
EVIDENCE_SCRIPT = SCRIPT.with_name('acceptance_evidence.py')
EGRESS_POLICY_SPEC = importlib.util.spec_from_file_location('self_host_egress_policy_contract', EGRESS_POLICY_CONTRACT)
assert EGRESS_POLICY_SPEC and EGRESS_POLICY_SPEC.loader
EGRESS_POLICY = importlib.util.module_from_spec(EGRESS_POLICY_SPEC)
EGRESS_POLICY_SPEC.loader.exec_module(EGRESS_POLICY)
RUNTIME_EVIDENCE_SCRIPT = SCRIPT.with_name('runtime-evidence.py')
PUBLIC_OBJECT_EVIDENCE_SCRIPT = SCRIPT.with_name('public_object_evidence.py')
SPEC = importlib.util.spec_from_file_location('self_host_volume_snapshot', SCRIPT)
assert SPEC and SPEC.loader
SNAPSHOT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SNAPSHOT)
EVIDENCE_SPEC = importlib.util.spec_from_file_location('self_host_acceptance_evidence', EVIDENCE_SCRIPT)
assert EVIDENCE_SPEC and EVIDENCE_SPEC.loader
EVIDENCE = importlib.util.module_from_spec(EVIDENCE_SPEC)
EVIDENCE_SPEC.loader.exec_module(EVIDENCE)
RUNTIME_EVIDENCE_SPEC = importlib.util.spec_from_file_location('self_host_runtime_evidence', RUNTIME_EVIDENCE_SCRIPT)
assert RUNTIME_EVIDENCE_SPEC and RUNTIME_EVIDENCE_SPEC.loader
RUNTIME_EVIDENCE = importlib.util.module_from_spec(RUNTIME_EVIDENCE_SPEC)
RUNTIME_EVIDENCE_SPEC.loader.exec_module(RUNTIME_EVIDENCE)
PUBLIC_OBJECT_EVIDENCE_SPEC = importlib.util.spec_from_file_location(
    'self_host_public_object_evidence', PUBLIC_OBJECT_EVIDENCE_SCRIPT
)
assert PUBLIC_OBJECT_EVIDENCE_SPEC and PUBLIC_OBJECT_EVIDENCE_SPEC.loader
PUBLIC_OBJECT_EVIDENCE = importlib.util.module_from_spec(PUBLIC_OBJECT_EVIDENCE_SPEC)
PUBLIC_OBJECT_EVIDENCE_SPEC.loader.exec_module(PUBLIC_OBJECT_EVIDENCE)
SOURCE_FREEZE_SCRIPT = SCRIPT.parent.parent.parent / 'backend' / 'scripts' / 'source_write_freeze.py'
SOURCE_FREEZE_SPEC = importlib.util.spec_from_file_location('self_host_source_write_freeze', SOURCE_FREEZE_SCRIPT)
assert SOURCE_FREEZE_SPEC and SOURCE_FREEZE_SPEC.loader
SOURCE_FREEZE = importlib.util.module_from_spec(SOURCE_FREEZE_SPEC)
SOURCE_FREEZE_SPEC.loader.exec_module(SOURCE_FREEZE)
CLEAN_SOURCE_ATTRIBUTION = {
    'git_commit': 'd' * 40,
    'git_tree': 'e' * 40,
    'worktree_clean': True,
}
EFFECTIVE_PROVIDER_CONFIGURATION = {
    'deployment_profile': 'self_hosted',
    'stt_prerecorded_model': 'mlx_moss_diarize',
    'mlx_moss_diarize_endpoint': 'http://host.docker.internal:5002/v1/audio/transcriptions',
    'mlx_moss_diarize_model': 'operator-model',
    'generic_llm_provider': 'generic',
    'generic_llm_model': 'operator-llm',
    'generic_llm_transport': 'openai_compatible_http',
    'generic_llm_endpoint_origin': 'https://llm.example.org',
    'embedding_provider': 'generic',
    'embedding_model': 'operator-embedding',
    'embedding_transport': 'direct',
    'embedding_dimension': '1536',
    'speaker_embedding_provider': 'sherpa_onnx',
    'speaker_embedding_model': '/models/speaker/speaker.onnx',
    'realtime_provider': 'relay',
    'realtime_model': 'operator-realtime',
    'realtime_transport': 'websocket_relay',
    'realtime_endpoint_origin': 'wss://realtime.example.org',
    'realtime_wire_protocol': 'openai_realtime_v1',
    'tts_provider': 'sherpa_onnx',
    'tts_model': 'model.onnx',
    'tts_transport': 'local',
    'tts_endpoint_origin': '',
    'file_chat_provider': 'local_extraction',
    'file_chat_model': 'operator-llm',
    'file_chat_transport': 'local_extraction',
    'app_icon_transport': 'local_template',
    'app_icon_endpoint_origin': '',
    'web_search_transport': 'searxng',
    'translation_provider': 'generic',
    'translation_model': 'operator-llm',
    'translation_transport': 'llm_feature_route',
    'translation_endpoint_origin': 'https://llm.example.org',
    'storage_backend': 'minio',
    'vector_store_provider': 'qdrant',
    'auth_provider': 'better_auth',
    'firmware_release_transport': 'manifest',
    'firmware_release_manifest_origin': 'https://objects.example.org',
    'firmware_release_asset_origin': 'https://objects.example.org',
    'desktop_update_legacy_fallback': 'disabled',
    'push_provider': 'disabled',
    'push_endpoint_origin': '',
    'push_model': 'disabled',
    'push_transport': 'disabled',
    'memory_keyword_provider': 'typesense',
    'conversation_keyword_provider': 'typesense',
    'typesense_transport': 'http',
    'typesense_host': 'typesense',
    'memory_typesense_collection': 'canonical_memory_atoms',
    'conversation_typesense_collection': 'omi_conversations',
}
EFFECTIVE_BACKEND_ENVIRONMENT = {
    'OMI_DEPLOYMENT_PROFILE': 'self_hosted',
    'STT_PRERECORDED_MODEL': 'mlx_moss_diarize',
    'MLX_MOSS_DIARIZE_ENDPOINT': 'http://host.docker.internal:5002/v1/audio/transcriptions',
    'MLX_MOSS_DIARIZE_MODEL': 'operator-model',
    'OMI_LLM_DEFAULT_PROVIDER': 'generic',
    'OMI_LLM_DEFAULT_MODEL': 'operator-llm',
    'TRANSLATION_PROVIDER': 'generic',
    'TRANSLATION_MODEL': 'operator-llm',
    'GENERIC_OPENAI_BASE_URL': 'https://llm.example.org/v1',
    'GENERIC_OPENAI_API_KEY': 'operator-secret',
    'EMBEDDING_PROVIDER': 'generic',
    'EMBEDDING_MODEL': 'operator-embedding',
    'EMBEDDING_CAPABILITY_TRANSPORT': 'direct',
    'EMBEDDING_DIMENSION': '1536',
    'APP_ICON_GENERATION_TRANSPORT': 'local_template',
    'SPEAKER_EMBEDDING_PROVIDER': 'sherpa_onnx',
    'SPEAKER_EMBEDDING_MODEL': '/models/speaker/speaker.onnx',
    'REALTIME_PROVIDER': 'relay',
    'REALTIME_MODEL': 'operator-realtime',
    'REALTIME_RELAY_URL': 'wss://realtime.example.org/v1/realtime',
    'REALTIME_RELAY_API_KEY': 'operator-realtime-secret',
    'REALTIME_RELAY_WIRE_PROTOCOL': 'openai_realtime_v1',
    'TTS_PROVIDER': 'sherpa_onnx',
    'TTS_SHERPA_MODEL': '/models/tts/model.onnx',
    'TTS_OPENAI_COMPATIBLE_BASE_URL': '',
    'TTS_OPENAI_COMPATIBLE_MODEL': '',
    'FILE_CHAT_TRANSPORT': 'local_extraction',
    'WEB_SEARCH_TRANSPORT': 'searxng',
    'STORAGE_BACKEND': 'minio',
    'VECTOR_STORE_PROVIDER': 'qdrant',
    'AUTH_PROVIDER': 'better_auth',
    'FIRMWARE_RELEASE_TRANSPORT': 'manifest',
    'FIRMWARE_RELEASE_MANIFEST_URL': 'https://objects.example.org/omi-firmware/releases.json',
    'FIRMWARE_RELEASE_ASSET_ORIGIN': 'https://objects.example.org',
    'DESKTOP_UPDATE_LEGACY_FALLBACK': 'disabled',
    'PUSH_PROVIDER': 'disabled',
    'MEMORY_KEYWORD_INDEX_PROVIDER': 'typesense',
    'CONVERSATION_KEYWORD_INDEX_PROVIDER': 'typesense',
    'TYPESENSE_PROTOCOL': 'http',
    'TYPESENSE_HOST': 'typesense',
    'TYPESENSE_API_KEY': 'operator-typesense-secret',
    'MEMORY_TYPESENSE_COLLECTION': 'canonical_memory_atoms',
    'CONVERSATION_TYPESENSE_COLLECTION': 'omi_conversations',
}
PASSED_RUNTIME_EVIDENCE = {
    'status': 'passed',
    'all_required_services_healthy': True,
    'runtime_identity': {
        'expected_git_commit': 'd' * 40,
        'expected_git_tree': 'e' * 40,
        'expected_config_sha256': 'c' * 64,
        'effective_provider_configuration': EFFECTIVE_PROVIDER_CONFIGURATION,
        'source_and_config_match': True,
        'workloads': {
            service: {
                'image_id': f'sha256:{service.encode().hex():0<64}'[:71],
                'source_git_commit': 'd' * 40,
                'source_git_tree': 'e' * 40,
                'runtime_config_sha256': 'c' * 64,
                'environment_matches_effective_config': True,
            }
            for service in ('auth-server', 'backend', 'queue-worker')
        },
    },
}
PASSED_RUNTIME_EVIDENCE['runtime_identity']['provider_attestation'] = RUNTIME_EVIDENCE.build_provider_attestation(
    expected_configuration=EFFECTIVE_PROVIDER_CONFIGURATION,
    runtime_configuration=EFFECTIVE_PROVIDER_CONFIGURATION,
    source={
        'image_id': PASSED_RUNTIME_EVIDENCE['runtime_identity']['workloads']['backend']['image_id'],
        'git_commit': 'd' * 40,
        'git_tree': 'e' * 40,
        'runtime_config_sha256': 'c' * 64,
    },
)
PASSED_RECOVERY_EVIDENCE = {
    'schema_version': 1,
    'status': 'passed',
    'scope': 'isolated_restore_host',
    'backup_manifest_sha256': 'f' * 64,
    'source_git_commit': 'd' * 40,
    'source_git_tree': 'e' * 40,
    'runtime_config_sha256': 'c' * 64,
    'backup_verified': True,
    'restore_completed': True,
    'post_restore_migration_passed': True,
    'post_restore_auth_smoke_passed': True,
    'post_restore_projection_checks_passed': True,
    'isolated_restore_host': True,
    'key_material_outside_backup': True,
    'production_kms_attested': True,
    'key_custody_reference': 'change-ticket/recovery-2026-08-20',
}


class SelfHostOperationsTest(unittest.TestCase):
    @staticmethod
    def _key_file(root: Path, value: bytes = b'k' * 32) -> Path:
        key_file = root / 'backup.key'
        key_file.write_bytes(value)
        key_file.chmod(0o600)
        return key_file

    def test_local_cutover_backend_trusts_the_generated_public_edge_ca(self) -> None:
        overlay = CUTOVER_OVERLAY.read_text(encoding='utf-8')
        self.assertIn('SSL_CERT_FILE=/etc/ssl/certs/omi-cutover-ca.crt', overlay)
        self.assertIn(
            '${CUTOVER_TLS_CERT_PATH:?CUTOVER_TLS_CERT_PATH is required}:' '/etc/ssl/certs/omi-cutover-ca.crt:ro',
            overlay,
        )

    def test_all_acceptance_compose_commands_use_the_clean_environment_wrapper(self) -> None:
        for script in (OPERATIONS, CUTOVER_GATE, ZERO_VENDOR_ACCEPTANCE):
            source = script.read_text(encoding='utf-8')
            self.assertIn('compose-clean-env.sh', source, script.name)
            self.assertNotIn('docker compose', source, script.name)
        runtime_source = RUNTIME_EVIDENCE_SCRIPT.read_text(encoding='utf-8')
        self.assertIn("with_name('compose-clean-env.sh')", runtime_source)
        self.assertNotIn("['docker', 'compose'", runtime_source)

    def test_runtime_evidence_keeps_validation_diagnostics_off_json_stdout(self) -> None:
        operations = OPERATIONS.read_text(encoding='utf-8')
        runtime_evidence = operations[
            operations.index('runtime_evidence() {') : operations.index('\n}', operations.index('runtime_evidence() {'))
        ]
        self.assertIn('require_runtime >&2', runtime_evidence)

    def test_runtime_evidence_extracts_complete_sanitized_provider_identity(self) -> None:
        effective = {'services': {'backend': {'environment': EFFECTIVE_BACKEND_ENVIRONMENT}}}

        configuration = RUNTIME_EVIDENCE.effective_provider_configuration(effective)

        self.assertEqual(configuration, EFFECTIVE_PROVIDER_CONFIGURATION)
        serialized = json.dumps(configuration, sort_keys=True)
        self.assertNotIn('operator-secret', serialized)
        self.assertNotIn('operator-realtime-secret', serialized)
        self.assertNotIn('operator-typesense-secret', serialized)
        self.assertNotIn('api_key', serialized.lower())

    def test_runtime_provider_attestation_records_models_and_effective_origins(self) -> None:
        source = {
            'image_id': 'sha256:' + 'a' * 64,
            'git_commit': 'd' * 40,
            'git_tree': 'e' * 40,
            'runtime_config_sha256': 'c' * 64,
        }
        attestation = RUNTIME_EVIDENCE.build_provider_attestation(
            expected_configuration=EFFECTIVE_PROVIDER_CONFIGURATION,
            runtime_configuration=EFFECTIVE_PROVIDER_CONFIGURATION,
            source=source,
        )
        self.assertEqual(attestation['schema_version'], 1)
        self.assertEqual(attestation['status'], 'passed')
        self.assertEqual(attestation['source'], source)
        self.assertEqual(attestation['providers']['generic_llm']['model'], 'operator-llm')
        self.assertEqual(attestation['providers']['generic_llm']['endpoint_origin'], 'https://llm.example.org')
        self.assertEqual(attestation['providers']['embedding']['model'], 'operator-embedding')
        self.assertEqual(attestation['providers']['embedding']['endpoint_origin'], 'https://llm.example.org')
        self.assertEqual(attestation['providers']['realtime']['model'], 'operator-realtime')
        self.assertEqual(attestation['providers']['realtime']['endpoint_origin'], 'wss://realtime.example.org')
        self.assertEqual(attestation['providers']['pre_recorded_stt']['model'], 'operator-model')
        self.assertEqual(attestation['providers']['pre_recorded_stt']['endpoint_path'], '/v1/audio/transcriptions')
        self.assertIsNone(attestation['external_service_revision'])
        self.assertIsNone(attestation['external_model_revision'])
        self.assertFalse(attestation['external_revision_attested'])
        RUNTIME_EVIDENCE.validate_provider_attestation(
            attestation,
            expected_configuration=EFFECTIVE_PROVIDER_CONFIGURATION,
            expected_source=source,
        )

    def test_runtime_provider_attestation_rejects_missing_model_official_host_and_fake_revision(self) -> None:
        source = {
            'image_id': 'sha256:' + 'a' * 64,
            'git_commit': 'd' * 40,
            'git_tree': 'e' * 40,
            'runtime_config_sha256': 'c' * 64,
        }
        missing_model = dict(EFFECTIVE_PROVIDER_CONFIGURATION)
        missing_model['generic_llm_model'] = ''
        with self.assertRaisesRegex(ValueError, 'missing generic_llm_model'):
            RUNTIME_EVIDENCE.build_provider_attestation(
                expected_configuration=missing_model,
                runtime_configuration=missing_model,
                source=source,
            )

        official = dict(EFFECTIVE_PROVIDER_CONFIGURATION)
        official['generic_llm_endpoint_origin'] = 'https://api.openai.com'
        with self.assertRaisesRegex(ValueError, 'forbidden official endpoint host'):
            RUNTIME_EVIDENCE.build_provider_attestation(
                expected_configuration=official,
                runtime_configuration=official,
                source=source,
            )

        attestation = RUNTIME_EVIDENCE.build_provider_attestation(
            expected_configuration=EFFECTIVE_PROVIDER_CONFIGURATION,
            runtime_configuration=EFFECTIVE_PROVIDER_CONFIGURATION,
            source=source,
        )
        attestation['external_service_revision'] = 'guessed-from-git'
        with self.assertRaisesRegex(ValueError, 'must not invent'):
            RUNTIME_EVIDENCE.validate_provider_attestation(attestation)

    def test_runtime_evidence_records_operator_webhook_without_secret_material(self) -> None:
        webhook_environment = {
            **EFFECTIVE_BACKEND_ENVIRONMENT,
            'PUSH_PROVIDER': 'webhook',
            'PUSH_WEBHOOK_URL': 'https://push.example.org/v1/omi/push',
            'PUSH_WEBHOOK_SECRET_FILE': '/run/secrets/omi-push-webhook',
        }
        configuration = RUNTIME_EVIDENCE.effective_provider_configuration(
            {'services': {'backend': {'environment': webhook_environment}}}
        )
        self.assertEqual(configuration['push_provider'], 'webhook')
        self.assertEqual(configuration['push_endpoint_origin'], 'https://push.example.org')
        self.assertEqual(configuration['push_model'], 'operator_webhook')
        self.assertEqual(configuration['push_transport'], 'https_json_hmac')
        RUNTIME_EVIDENCE._validate_provider_configuration(configuration)
        self.assertNotIn('secret', json.dumps(configuration).lower())

        webhook_environment['PUSH_WEBHOOK_URL'] = 'http://push.example.org/v1/omi/push'
        with self.assertRaisesRegex(ValueError, 'credential-free endpoint'):
            RUNTIME_EVIDENCE.effective_provider_configuration(
                {'services': {'backend': {'environment': webhook_environment}}}
            )

    def test_clean_compose_wrapper_removes_deployment_overrides_and_preserves_only_gate_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / 'production.env'
            env_file.write_text(
                '\n'.join(
                    (
                        'MLX_MOSS_DIARIZE_ENDPOINT=http://reviewed.internal/v1/audio/transcriptions',
                        'MLX_MOSS_DIARIZE_MODEL=reviewed-model',
                        'SENSEVOICE_MODEL_HOST_PATH=/reviewed/sensevoice',
                        'TTS_MODEL_HOST_DIR=/reviewed/tts',
                        'GENERIC_OPENAI_BASE_URL=https://reviewed.example.org/v1',
                    )
                )
                + '\n',
                encoding='utf-8',
            )
            compose_file = root / 'compose.yml'
            compose_file.write_text('services: {}\n', encoding='utf-8')
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            call_log = root / 'docker.calls'
            docker = bin_dir / 'docker'
            docker.write_text(
                '#!/bin/sh\n'
                'printf "%s|mlx_endpoint=%s|mlx_model=%s|sensevoice=%s|tts=%s|llm=%s|commit=%s|cutover=%s\\n" '
                '"$*" "${MLX_MOSS_DIARIZE_ENDPOINT-unset}" "${MLX_MOSS_DIARIZE_MODEL-unset}" '
                '"${SENSEVOICE_MODEL_HOST_PATH-unset}" "${TTS_MODEL_HOST_DIR-unset}" '
                '"${GENERIC_OPENAI_BASE_URL-unset}" "${OMI_SOURCE_GIT_COMMIT-unset}" '
                '"${CUTOVER_HTTPS_PORT-unset}" > "$FAKE_DOCKER_CALLS"\n',
                encoding='utf-8',
            )
            docker.chmod(0o755)
            environment = {
                **os.environ,
                'PATH': f'{bin_dir}:{os.environ["PATH"]}',
                'FAKE_DOCKER_CALLS': str(call_log),
                'MLX_MOSS_DIARIZE_ENDPOINT': 'https://host-injected.example/v1/audio/transcriptions',
                'MLX_MOSS_DIARIZE_MODEL': 'host-injected-model',
                'SENSEVOICE_MODEL_HOST_PATH': '/host-injected/sensevoice',
                'TTS_MODEL_HOST_DIR': '/host-injected/tts',
                'GENERIC_OPENAI_BASE_URL': 'https://host-injected.example/v1',
                'OMI_SOURCE_GIT_COMMIT': 'd' * 40,
                'CUTOVER_HTTPS_PORT': '18443',
            }
            result = subprocess.run(
                ['bash', str(COMPOSE_WRAPPER), str(env_file), str(compose_file), 'config', '--quiet'],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            call = call_log.read_text(encoding='utf-8')
            self.assertIn(f'compose --env-file {env_file} --file {compose_file} config --quiet', call)
            for key in ('mlx_endpoint', 'mlx_model', 'sensevoice', 'tts', 'llm'):
                self.assertIn(f'{key}=unset', call)
            self.assertIn(f'commit={"d" * 40}', call)
            self.assertIn('cutover=18443', call)

    def test_public_object_acceptance_uses_signed_put_get_delete_on_exact_origin(self) -> None:
        payload = b'public-object-cutover:marker'

        class Blob:
            present = False

            def generate_signed_url(self, *, expiration, method):
                self.last_method = method
                return f'https://objects.example.org/private/item?method={method}'

            def exists(self):
                return self.present

            def delete(self):
                self.present = False

        blob = Blob()
        storage_client = SimpleNamespace(
            bucket=lambda name: SimpleNamespace(blob=lambda path: blob),
        )

        class Client:
            def put(self, url, *, content, headers):
                self.put_url = url
                blob.present = True
                self.content = content
                return SimpleNamespace(status_code=200)

            def get(self, url):
                self.get_url = url
                return SimpleNamespace(status_code=200, content=self.content)

            def delete(self, url):
                self.delete_url = url
                blob.present = False
                return SimpleNamespace(status_code=204)

        client = Client()
        previous_bucket = os.environ.get('BUCKET_TEMPORAL_SYNC_LOCAL')
        os.environ['BUCKET_TEMPORAL_SYNC_LOCAL'] = 'private'
        try:
            result = PUBLIC_OBJECT_EVIDENCE.public_signed_object_crud(
                client,
                objects_url='https://objects.example.org',
                marker='marker',
                storage_client=storage_client,
            )
        finally:
            if previous_bucket is None:
                os.environ.pop('BUCKET_TEMPORAL_SYNC_LOCAL', None)
            else:
                os.environ['BUCKET_TEMPORAL_SYNC_LOCAL'] = previous_bucket
        self.assertEqual(result['status'], 'passed')
        self.assertIn('method=PUT', client.put_url)
        self.assertIn('method=GET', client.get_url)
        self.assertIn('method=DELETE', client.delete_url)
        self.assertFalse(blob.present)

        blob.generate_signed_url = lambda **kwargs: 'https://wrong.example.org/private/item?signature=x'
        os.environ['BUCKET_TEMPORAL_SYNC_LOCAL'] = 'private'
        try:
            with self.assertRaisesRegex(RuntimeError, 'did not use PUBLIC_OBJECTS_URL'):
                PUBLIC_OBJECT_EVIDENCE.public_signed_object_crud(
                    client,
                    objects_url='https://objects.example.org',
                    marker='wrong-origin',
                    storage_client=storage_client,
                )
        finally:
            if previous_bucket is None:
                os.environ.pop('BUCKET_TEMPORAL_SYNC_LOCAL', None)
            else:
                os.environ['BUCKET_TEMPORAL_SYNC_LOCAL'] = previous_bucket

        for malformed_origin in (
            'http://objects.example.org',
            'https://objects.example.org/prefix',
            'https://objects.example.org?override=1',
            'https://user:password@objects.example.org',
        ):
            os.environ['BUCKET_TEMPORAL_SYNC_LOCAL'] = 'private'
            try:
                with self.assertRaisesRegex(RuntimeError, 'exact HTTPS origin'):
                    PUBLIC_OBJECT_EVIDENCE.public_signed_object_crud(
                        client,
                        objects_url=malformed_origin,
                        marker='malformed-origin',
                        storage_client=storage_client,
                    )
            finally:
                if previous_bucket is None:
                    os.environ.pop('BUCKET_TEMPORAL_SYNC_LOCAL', None)
                else:
                    os.environ['BUCKET_TEMPORAL_SYNC_LOCAL'] = previous_bucket

    def test_runtime_evidence_rejects_stale_images_config_and_unhealthy_services(self) -> None:
        services = {
            service: {'state': 'running', 'health': 'healthy'} for service in RUNTIME_EVIDENCE.REQUIRED_SERVICES
        }
        for service in RUNTIME_EVIDENCE.SOURCE_WORKLOADS:
            services[service].update(
                {
                    'image_id': f'sha256:{service.encode().hex():0<64}'[:71],
                    'source_git_commit': 'd' * 40,
                    'source_git_tree': 'e' * 40,
                    'runtime_config_sha256': 'c' * 64,
                    'environment_matches_effective_config': True,
                }
            )

        result = RUNTIME_EVIDENCE.validate_runtime_snapshot(
            services=services,
            expected_git_commit='d' * 40,
            expected_git_tree='e' * 40,
            expected_config_sha256='c' * 64,
            effective_provider_configuration=EFFECTIVE_PROVIDER_CONFIGURATION,
        )
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['all_required_services_healthy'])
        self.assertEqual(set(result['runtime_identity']['workloads']), set(RUNTIME_EVIDENCE.SOURCE_WORKLOADS))
        self.assertEqual(
            result['runtime_identity']['effective_provider_configuration'], EFFECTIVE_PROVIDER_CONFIGURATION
        )

        with self.assertRaisesRegex(ValueError, 'effective provider configuration'):
            RUNTIME_EVIDENCE.validate_runtime_snapshot(
                services=services,
                expected_git_commit='d' * 40,
                expected_git_tree='e' * 40,
                expected_config_sha256='c' * 64,
                effective_provider_configuration={
                    **EFFECTIVE_PROVIDER_CONFIGURATION,
                    'mlx_moss_diarize_model': '',
                },
            )

        for key, value in (
            ('generic_llm_endpoint_origin', 'https://operator:secret@llm.example.org'),
            ('realtime_endpoint_origin', 'wss://relay.example.org/path'),
            ('generic_llm_endpoint_origin', 'https://llm.example.org:invalid'),
        ):
            unsafe_provider_config = dict(EFFECTIVE_PROVIDER_CONFIGURATION)
            unsafe_provider_config[key] = value
            with self.assertRaisesRegex(ValueError, 'sanitized endpoint|credential-free endpoint'):
                RUNTIME_EVIDENCE.validate_runtime_snapshot(
                    services=services,
                    expected_git_commit='d' * 40,
                    expected_git_tree='e' * 40,
                    expected_config_sha256='c' * 64,
                    effective_provider_configuration=unsafe_provider_config,
                )

        for key, value, message in (
            ('generic_llm_model', 123, 'missing generic_llm_model'),
            ('embedding_dimension', '0', 'positive integer'),
            ('typesense_host', 'https://typesense.example.org', 'host name'),
            ('speaker_embedding_provider', 'http', 'local sherpa_onnx'),
            ('app_icon_transport', 'gateway', 'unsupported app icon'),
            ('web_search_transport', 'gateway', 'local SearXNG'),
            ('translation_provider', 'gemini', 'unsupported translation'),
            ('storage_backend', 'gcs', 'MinIO'),
            ('vector_store_provider', 'pinecone', 'Qdrant'),
            ('auth_provider', 'firebase', 'Better Auth'),
            ('firmware_release_transport', 'gcs', 'manifest transport'),
            ('desktop_update_legacy_fallback', 'enabled', 'legacy vendor fallback'),
        ):
            unsafe_provider_config = dict(EFFECTIVE_PROVIDER_CONFIGURATION)
            unsafe_provider_config[key] = value
            with self.assertRaisesRegex(ValueError, message):
                RUNTIME_EVIDENCE.validate_runtime_snapshot(
                    services=services,
                    expected_git_commit='d' * 40,
                    expected_git_tree='e' * 40,
                    expected_config_sha256='c' * 64,
                    effective_provider_configuration=unsafe_provider_config,
                )

        unsafe_provider_config = dict(EFFECTIVE_PROVIDER_CONFIGURATION)
        unsafe_provider_config['GENERIC_OPENAI_API_KEY'] = 'operator-secret'
        with self.assertRaisesRegex(ValueError, 'incomplete or unexpected identity shape'):
            RUNTIME_EVIDENCE.validate_runtime_snapshot(
                services=services,
                expected_git_commit='d' * 40,
                expected_git_tree='e' * 40,
                expected_config_sha256='c' * 64,
                effective_provider_configuration=unsafe_provider_config,
            )

        stale = json.loads(json.dumps(services))
        stale['backend']['source_git_tree'] = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'source identity'):
            RUNTIME_EVIDENCE.validate_runtime_snapshot(
                services=stale,
                expected_git_commit='d' * 40,
                expected_git_tree='e' * 40,
                expected_config_sha256='c' * 64,
                effective_provider_configuration=EFFECTIVE_PROVIDER_CONFIGURATION,
            )

        wrong_config = json.loads(json.dumps(services))
        wrong_config['queue-worker']['runtime_config_sha256'] = 'a' * 64
        with self.assertRaisesRegex(ValueError, 'runtime config identity'):
            RUNTIME_EVIDENCE.validate_runtime_snapshot(
                services=wrong_config,
                expected_git_commit='d' * 40,
                expected_git_tree='e' * 40,
                expected_config_sha256='c' * 64,
                effective_provider_configuration=EFFECTIVE_PROVIDER_CONFIGURATION,
            )

        unhealthy = json.loads(json.dumps(services))
        unhealthy['redis']['health'] = 'unhealthy'
        with self.assertRaisesRegex(ValueError, 'redis is not running and healthy'):
            RUNTIME_EVIDENCE.validate_runtime_snapshot(
                services=unhealthy,
                expected_git_commit='d' * 40,
                expected_git_tree='e' * 40,
                expected_config_sha256='c' * 64,
                effective_provider_configuration=EFFECTIVE_PROVIDER_CONFIGURATION,
            )

    def test_runtime_evidence_rejects_vendor_bindings_in_running_workload(self) -> None:
        with self.assertRaisesRegex(RuntimeError, 'OPENAI_API_KEY'):
            RUNTIME_EVIDENCE.validate_runtime_environment({'OPENAI_API_KEY': 'redacted'})

        with self.assertRaisesRegex(RuntimeError, 'ANTHROPIC_API_KEY'):
            RUNTIME_EVIDENCE.validate_runtime_environment({'ANTHROPIC_API_KEY': 'redacted'})

        with self.assertRaisesRegex(RuntimeError, 'official endpoint host'):
            RUNTIME_EVIDENCE.validate_runtime_environment({'GENERIC_OPENAI_BASE_URL': 'https://api.openai.com/v1'})

        # Runtime evidence diagnostics must identify only the binding class,
        # not leak an operator credential from the container environment.
        with self.assertRaisesRegex(RuntimeError, 'official endpoint host') as context:
            RUNTIME_EVIDENCE.validate_runtime_environment({'LLM_ENDPOINT': 'https://api.openai.com/v1?key=secret'})
        self.assertNotIn('secret', str(context.exception))

    def test_attributed_start_builds_current_images_before_service_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_dir = root / 'sensevoice'
            model_dir.mkdir()
            (model_dir / 'model.int8.onnx').write_bytes(b'fixture')
            (model_dir / 'tokens.txt').write_text('fixture\n', encoding='utf-8')
            speaker_model_dir = root / 'speaker'
            speaker_model_dir.mkdir()
            (speaker_model_dir / 'speaker.onnx').write_bytes(b'fixture')
            diarization_audio = root / 'two-speaker.wav'
            diarization_audio.write_bytes(b'RIFF-fixture')
            tts_model_dir = root / 'tts'
            tts_model_dir.mkdir()
            (tts_model_dir / 'model.onnx').write_bytes(b'fixture')
            (tts_model_dir / 'tokens.txt').write_text('fixture\n', encoding='utf-8')
            (tts_model_dir / 'espeak-ng-data').mkdir()
            env_lines = []
            for line in (SCRIPT.parent / '.env.production.example').read_text(encoding='utf-8').splitlines():
                if '=REPLACE_' in line:
                    key = line.split('=', 1)[0]
                    line = f'{key}=test-{key.lower()}-value-with-sufficient-length'
                line = line.replace('example.com', 'operator.example.org')
                if line.startswith('SENSEVOICE_MODEL_HOST_PATH='):
                    line = f'SENSEVOICE_MODEL_HOST_PATH={model_dir}'
                if line.startswith('SPEAKER_MODEL_HOST_DIR='):
                    line = f'SPEAKER_MODEL_HOST_DIR={speaker_model_dir}'
                if line.startswith('MLX_MOSS_DIARIZE_ACCEPTANCE_WAV_HOST_PATH='):
                    line = f'MLX_MOSS_DIARIZE_ACCEPTANCE_WAV_HOST_PATH={diarization_audio}'
                if line.startswith('TTS_MODEL_HOST_DIR='):
                    line = f'TTS_MODEL_HOST_DIR={tts_model_dir}'
                env_lines.append(line)
            env_file = root / 'production.env'
            env_file.write_text('\n'.join(env_lines) + '\n', encoding='utf-8')
            effective_fixture = {
                'services': {
                    service: {'labels': {'com.omi.runtime.config-sha256': '0' * 64}}
                    for service in RUNTIME_EVIDENCE.SOURCE_WORKLOADS
                }
            }
            effective_fixture_json = json.dumps(effective_fixture, separators=(',', ':'))
            config_sha256 = RUNTIME_EVIDENCE.canonical_effective_config_sha256(effective_fixture)

            bin_dir = root / 'bin'
            bin_dir.mkdir()
            call_log = root / 'docker.calls'
            docker = bin_dir / 'docker'
            docker.write_text(
                '#!/bin/sh\nprintf "%s|TTS_PROVIDER=%s|MLX_ENDPOINT=%s|MLX_MODEL=%s|SENSEVOICE_PATH=%s|SPEAKER_PATH=%s|TTS_PATH=%s|LLM_URL=%s\\n" '
                '"$*" "${TTS_PROVIDER-unset}" "${MLX_MOSS_DIARIZE_ENDPOINT-unset}" '
                '"${MLX_MOSS_DIARIZE_MODEL-unset}" "${SENSEVOICE_MODEL_HOST_PATH-unset}" '
                '"${SPEAKER_MODEL_HOST_DIR-unset}" "${TTS_MODEL_HOST_DIR-unset}" '
                '"${GENERIC_OPENAI_BASE_URL-unset}" >> "$FAKE_DOCKER_CALLS"\n'
                'if [ "$1" = "compose" ]; then\n'
                f'  case "$*" in *" config --format json"*) printf "%s\\n" \'{effective_fixture_json}\'; exit 0;; esac\n'
                '  case "$*" in *" ps --quiet "*) for last in "$@"; do :; done; printf "container-%s\\n" "$last";; esac\n'
                'elif [ "$1" = "inspect" ]; then printf "running healthy\\n"; fi\n'
                'exit 0\n',
                encoding='utf-8',
            )
            docker.chmod(0o755)
            environment = {
                **os.environ,
                'PATH': f'{bin_dir}:{os.environ["PATH"]}',
                'SELF_HOST_ENV': str(env_file),
                'SELF_HOST_REQUIRE_ATTESTED_BUILD': 'true',
                'OMI_SOURCE_GIT_COMMIT': 'd' * 40,
                'OMI_SOURCE_GIT_TREE': 'e' * 40,
                'OMI_RUNTIME_CONFIG_SHA256': config_sha256,
                'FAKE_DOCKER_CALLS': str(call_log),
                'PYTHON': sys.executable,
                # Compose normally lets this shell binding override --env-file.
                # The attributed wrapper must remove it before config/build/up.
                'TTS_PROVIDER': 'openai_compatible',
                'MLX_MOSS_DIARIZE_ENDPOINT': 'https://host-injected.example/v1/audio/transcriptions',
                'MLX_MOSS_DIARIZE_MODEL': 'host-injected-model',
                'SENSEVOICE_MODEL_HOST_PATH': '/host-injected/sensevoice',
                'SPEAKER_MODEL_HOST_DIR': '/host-injected/speaker',
                'TTS_MODEL_HOST_DIR': '/host-injected/tts',
                'GENERIC_OPENAI_BASE_URL': 'https://host-injected.example/v1',
            }
            started = subprocess.run(
                ['bash', str(OPERATIONS), 'start'],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            calls = call_log.read_text(encoding='utf-8')
            compose_calls = [line for line in calls.splitlines() if line.startswith('compose ')]
            self.assertTrue(compose_calls)
            for key in (
                'TTS_PROVIDER',
                'MLX_ENDPOINT',
                'MLX_MODEL',
                'SENSEVOICE_PATH',
                'SPEAKER_PATH',
                'TTS_PATH',
                'LLM_URL',
            ):
                self.assertTrue(all(f'{key}=unset' in line for line in compose_calls), key)
            build = calls.index(' build --pull auth-server backend')
            first_up = calls.index(' up --detach --wait postgres')
            self.assertLess(build, first_up)

            rejected = subprocess.run(
                ['bash', str(OPERATIONS), 'start'],
                check=False,
                capture_output=True,
                text=True,
                env={**environment, 'OMI_RUNTIME_CONFIG_SHA256': 'a' * 64},
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn('reviewed environment changed before attributed build', rejected.stderr)

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
            diarization_audio = root / 'two-speaker.wav'
            diarization_audio.write_bytes(b'RIFF-fixture')
            env_file = root / 'production.env'
            env_file.write_text(
                '\n'.join(
                    (
                        'BETTER_AUTH_TRUSTED_ORIGINS=https://app.omi.test',
                        'PUBLIC_BACKEND_URL=https://api.omi.test',
                        'PUBLIC_AUTH_URL=https://auth.omi.test',
                        'PUBLIC_MCP_URL=https://mcp.omi.test',
                        'PUBLIC_OBJECTS_URL=https://objects.omi.test',
                        f'MLX_MOSS_DIARIZE_ACCEPTANCE_WAV_HOST_PATH={diarization_audio}',
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
                '#!/bin/sh\nprintf "%s|CUTOVER_PORT=%s|CUTOVER_CERT=%s|CUTOVER_KEY=%s\\n" '
                '"$*" "${CUTOVER_HTTPS_PORT-unset}" "${CUTOVER_TLS_CERT_PATH-unset}" '
                '"${CUTOVER_TLS_KEY_PATH-unset}" >> "$FAKE_DOCKER_CALLS"\n'
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
                'CUTOVER_TLS_CERT_PATH': '/host-injected/cert.pem',
                'CUTOVER_TLS_KEY_PATH': '/host-injected/key.pem',
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
            self.assertIn('--env PUBLIC_OBJECTS_URL=https://objects.omi.test', calls)
            self.assertNotIn('PUBLIC_AUTH_URL=https://auth.omi.test:18443', calls)
            overlay_calls = [
                line
                for line in calls.splitlines()
                if f'--file {SCRIPT.parent / "compose.cutover-acceptance.yml"}' in line
            ]
            self.assertTrue(overlay_calls)
            self.assertTrue(all('CUTOVER_PORT=18443' in line for line in overlay_calls))
            self.assertTrue(all('omi-cutover-tls.' in line and '/server.crt' in line for line in overlay_calls))
            self.assertTrue(all('omi-cutover-tls.' in line and '/server.key' in line for line in overlay_calls))
            self.assertTrue(all('/host-injected/' not in line for line in overlay_calls))

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
            'https_origin_and_hairpin': {
                'mode': 'external',
                'trust_source': 'system_ca',
                'certificate_chain_verified': True,
                'public_backend_url': 'https://api.example.org',
                'public_auth_url': 'https://auth.example.org',
                'public_mcp_url': 'https://mcp.example.org',
                'public_objects_url': 'https://objects.example.org',
                'jwt_issuer_audience_exact': True,
                'public_jwks_kid_present': True,
                'backend_private_jwks_verification': True,
                'auth_private_lifecycle_blocked_at_edge': True,
                'wss_public_origin_exercised': True,
                'public_object_signed_crud': {'status': 'passed'},
            },
            'assembled_product_loop': {
                'capture': {
                    'fixture_manifest_match': True,
                    'speaker_embedding': {'status': 'passed'},
                    'speaker_diarization': {
                        'status': 'passed',
                        'provider': 'mlx_moss_diarize',
                        'route': {
                            'endpoint_origin': 'http://host.docker.internal:5002',
                            'transcription_path': '/v1/audio/transcriptions',
                            'models_catalog_path': '/v1/models',
                            'multipart_model': 'operator-model',
                            'response_format': 'verbose_json',
                            'authorization': 'none',
                        },
                        'configured_model': 'operator-model',
                        'model_catalog_exact_id_match': True,
                        'real_transcription_route_exercised': True,
                        'audio_sha256': 'b' * 64,
                        'audio_duration_seconds': 111.5,
                        'segment_count': 27,
                        'speaker_count': 2,
                        'speaker_transition_count': 3,
                        'audio_duration_source': 'wav_header_frames_divided_by_sample_rate',
                        'service_revision_reported': False,
                        'operator_model_source_attested_by_gate': False,
                    },
                    'mounted_model_artifact_identity': {
                        'status': 'passed',
                        'paths_recorded': False,
                        'artifacts': {
                            name: {'sha256': 'a' * 64, 'bytes': 1}
                            for name in (
                                'sensevoice_model',
                                'sensevoice_tokens',
                                'speaker_embedding_model',
                                'tts_model',
                                'tts_tokens',
                            )
                        },
                    },
                },
                'realtime_relay': {'status': 'passed'},
                'tts': {'status': 'passed'},
                'app_icon': {'status': 'passed'},
                'file_chat': {'status': 'passed'},
                'typesense_keyword': {'status': 'passed'},
                'conversation_typesense': {'status': 'passed'},
                'firmware': {'status': 'passed'},
                'remember': {'long_term_admission': 'passed'},
            },
            'live_egress': {
                'enforcement': 'not_enforced_by_compose',
                'sentinel_targets_denied': [],
                'workloads': [],
                'operator_policy_artifact_sha256': None,
                'operator_policy_schema_version': None,
                'operator_policy_workloads': [],
                'operator_policy_denied_targets': [],
            },
        }
        local = EVIDENCE.build_evidence(
            mode='cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=assembled,
            checked_at='2026-08-20T00:00:00+00:00',
            runtime_evidence=PASSED_RUNTIME_EVIDENCE,
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
            runtime_evidence=PASSED_RUNTIME_EVIDENCE,
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
            'operator_policy_schema_version': 1,
            'operator_policy_workloads': ['auth-server', 'backend', 'queue-worker'],
            'operator_policy_denied_targets': [
                '1.1.1.1',
                'api.openai.com',
                'api.omi.me',
                'api.anthropic.com',
                'generativelanguage.googleapis.com',
            ],
            'scope': 'sentinel_targets_only',
        }
        external_with_policy = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=assembled,
            checked_at='2026-08-20T00:00:00+00:00',
            runtime_evidence=PASSED_RUNTIME_EVIDENCE,
            recovery_evidence=PASSED_RECOVERY_EVIDENCE,
        )
        self.assertTrue(external_with_policy['authorizes_production_cutover'])
        self.assertIsNone(external_with_policy['remaining_cutover_reason'])

        without_external_edge = json.loads(json.dumps(assembled))
        without_external_edge['https_origin_and_hairpin']['mode'] = 'local'
        rejected_local_edge = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=without_external_edge,
            checked_at='2026-08-20T00:00:00+00:00',
            runtime_evidence=PASSED_RUNTIME_EVIDENCE,
            recovery_evidence=PASSED_RECOVERY_EVIDENCE,
        )
        self.assertFalse(rejected_local_edge['authorizes_production_cutover'])
        self.assertEqual(
            rejected_local_edge['remaining_cutover_reason'],
            'external_public_edge_certificate_or_origin_not_verified',
        )

        missing_recovery = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=assembled,
            checked_at='2026-08-20T00:00:00+00:00',
            runtime_evidence=PASSED_RUNTIME_EVIDENCE,
        )
        self.assertFalse(missing_recovery['authorizes_production_cutover'])
        self.assertEqual(
            missing_recovery['remaining_cutover_reason'], 'external_backup_restore_or_kms_evidence_missing'
        )

        partial_runtime = json.loads(json.dumps(PASSED_RUNTIME_EVIDENCE))
        del partial_runtime['runtime_identity']['workloads']['queue-worker']
        rejected_partial_runtime = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=assembled,
            checked_at='2026-08-20T00:00:00+00:00',
            runtime_evidence=partial_runtime,
            recovery_evidence=PASSED_RECOVERY_EVIDENCE,
        )
        self.assertFalse(rejected_partial_runtime['authorizes_tested_configuration_cutover'])
        self.assertEqual(
            rejected_partial_runtime['remaining_cutover_reason'],
            'production_service_health_or_runtime_identity_not_passed',
        )

        without_objects = json.loads(json.dumps(assembled))
        without_objects['https_origin_and_hairpin']['public_object_signed_crud']['status'] = 'failed'
        missing_object_edge = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=without_objects,
            checked_at='2026-08-20T00:00:00+00:00',
            runtime_evidence=PASSED_RUNTIME_EVIDENCE,
        )
        self.assertFalse(missing_object_edge['authorizes_tested_configuration_cutover'])
        self.assertEqual(missing_object_edge['remaining_cutover_reason'], 'public_object_signed_crud_not_passed')

        unhealthy_runtime = {**PASSED_RUNTIME_EVIDENCE, 'all_required_services_healthy': False}
        missing_runtime_health = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=assembled,
            checked_at='2026-08-20T00:00:00+00:00',
            runtime_evidence=unhealthy_runtime,
        )
        self.assertFalse(missing_runtime_health['authorizes_tested_configuration_cutover'])
        self.assertEqual(
            missing_runtime_health['remaining_cutover_reason'],
            'production_service_health_or_runtime_identity_not_passed',
        )

        without_speaker = json.loads(json.dumps(assembled))
        without_speaker['assembled_product_loop']['capture']['speaker_embedding']['status'] = 'failed'
        missing_speaker = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=without_speaker,
            checked_at='2026-08-20T00:00:00+00:00',
            runtime_evidence=PASSED_RUNTIME_EVIDENCE,
        )
        self.assertFalse(missing_speaker['authorizes_tested_configuration_cutover'])
        self.assertEqual(missing_speaker['remaining_cutover_reason'], 'speaker_embedding_not_passed')

        without_diarization = json.loads(json.dumps(assembled))
        without_diarization['assembled_product_loop']['capture'].pop('speaker_diarization')
        missing_diarization = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=without_diarization,
            checked_at='2026-08-20T00:00:00+00:00',
            runtime_evidence=PASSED_RUNTIME_EVIDENCE,
        )
        self.assertFalse(missing_diarization['authorizes_tested_configuration_cutover'])
        self.assertEqual(missing_diarization['remaining_cutover_reason'], 'speaker_diarization_not_passed')

        missing_diarization_hard_field = json.loads(json.dumps(assembled))
        missing_diarization_hard_field['assembled_product_loop']['capture']['speaker_diarization']['route'].pop(
            'multipart_model'
        )
        rejected_diarization_hard_field = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=missing_diarization_hard_field,
            checked_at='2026-08-20T00:00:00+00:00',
            runtime_evidence=PASSED_RUNTIME_EVIDENCE,
        )
        self.assertFalse(rejected_diarization_hard_field['authorizes_tested_configuration_cutover'])
        self.assertEqual(
            rejected_diarization_hard_field['remaining_cutover_reason'],
            'speaker_diarization_not_passed',
        )

        for key, injected_value in (
            ('mlx_moss_diarize_model', 'host-injected-model'),
            ('mlx_moss_diarize_endpoint', 'https://host-injected.example/v1/audio/transcriptions'),
        ):
            mismatched_runtime = json.loads(json.dumps(PASSED_RUNTIME_EVIDENCE))
            mismatched_runtime['runtime_identity']['effective_provider_configuration'][key] = injected_value
            rejected_runtime_binding = EVIDENCE.build_evidence(
                mode='external-cutover-live',
                source_attribution=CLEAN_SOURCE_ATTRIBUTION,
                live_replacement={'status': 'passed'},
                assembled_loop=assembled,
                checked_at='2026-08-20T00:00:00+00:00',
                runtime_evidence=mismatched_runtime,
            )
            self.assertFalse(rejected_runtime_binding['authorizes_tested_configuration_cutover'], key)
            self.assertEqual(
                rejected_runtime_binding['remaining_cutover_reason'],
                'speaker_diarization_runtime_config_binding_not_passed',
            )

        without_model_artifact_identity = json.loads(json.dumps(assembled))
        without_model_artifact_identity['assembled_product_loop']['capture'].pop('mounted_model_artifact_identity')
        missing_model_artifact_identity = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=CLEAN_SOURCE_ATTRIBUTION,
            live_replacement={'status': 'passed'},
            assembled_loop=without_model_artifact_identity,
            checked_at='2026-08-20T00:00:00+00:00',
            runtime_evidence=PASSED_RUNTIME_EVIDENCE,
        )
        self.assertFalse(missing_model_artifact_identity['authorizes_tested_configuration_cutover'])
        self.assertEqual(
            missing_model_artifact_identity['remaining_cutover_reason'],
            'mounted_model_artifact_identity_not_passed',
        )

        for capability in (
            'realtime_relay',
            'tts',
            'app_icon',
            'file_chat',
            'typesense_keyword',
            'conversation_typesense',
            'firmware',
        ):
            missing_status_field = json.loads(json.dumps(assembled))
            missing_status_field['assembled_product_loop'][capability].pop('status')
            rejected_hard_field = EVIDENCE.build_evidence(
                mode='external-cutover-live',
                source_attribution=CLEAN_SOURCE_ATTRIBUTION,
                live_replacement={'status': 'passed'},
                assembled_loop=missing_status_field,
                checked_at='2026-08-20T00:00:00+00:00',
                runtime_evidence=PASSED_RUNTIME_EVIDENCE,
            )
            self.assertFalse(rejected_hard_field['authorizes_tested_configuration_cutover'], capability)
            self.assertEqual(rejected_hard_field['remaining_cutover_reason'], f'{capability}_not_passed')

        dirty_source = {**CLEAN_SOURCE_ATTRIBUTION, 'git_tree': 'f' * 40, 'worktree_clean': False}
        dirty_external = EVIDENCE.build_evidence(
            mode='external-cutover-live',
            source_attribution=dirty_source,
            live_replacement={'status': 'passed'},
            assembled_loop=assembled,
            checked_at='2026-08-20T00:00:00+00:00',
            runtime_evidence=PASSED_RUNTIME_EVIDENCE,
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
                runtime_evidence=PASSED_RUNTIME_EVIDENCE,
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
                runtime_evidence=PASSED_RUNTIME_EVIDENCE,
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
            runtime_evidence=PASSED_RUNTIME_EVIDENCE,
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
            runtime_evidence=PASSED_RUNTIME_EVIDENCE,
        )
        self.assertFalse(non_cutover_mode['authorizes_tested_configuration_cutover'])
        self.assertFalse(non_cutover_mode['authorizes_production_cutover'])

    def test_external_cutover_requires_policy_artifact_and_probes_all_sentinels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            searxng_secret = 'acceptance-test-secret'
            searxng_secret_sha256 = hashlib.sha256(searxng_secret.encode()).hexdigest()
            diarization_audio = root / 'two-speaker.wav'
            diarization_audio.write_bytes(b'RIFF-fixture')
            env_file = root / 'production.env'
            env_file.write_text(
                '\n'.join(
                    (
                        'BETTER_AUTH_TRUSTED_ORIGINS=https://app.example.org',
                        'PUBLIC_BACKEND_URL=https://api.example.org',
                        'PUBLIC_AUTH_URL=https://auth.example.org',
                        'PUBLIC_MCP_URL=https://mcp.example.org',
                        'PUBLIC_OBJECTS_URL=https://objects.example.org',
                        f'MLX_MOSS_DIARIZE_ACCEPTANCE_WAV_HOST_PATH={diarization_audio}',
                        'MLX_MOSS_DIARIZE_ENDPOINT=http://reviewed.internal/v1/audio/transcriptions',
                        'MLX_MOSS_DIARIZE_MODEL=reviewed-model',
                        'SENSEVOICE_MODEL_HOST_PATH=/reviewed/sensevoice',
                        'SPEAKER_MODEL_HOST_DIR=/reviewed/speaker',
                        'TTS_MODEL_HOST_DIR=/reviewed/tts',
                        'GENERIC_OPENAI_BASE_URL=https://reviewed.example.org/v1',
                        f'SEARXNG_SECRET={searxng_secret}',
                    )
                )
                + '\n',
                encoding='utf-8',
            )
            policy = root / 'egress-policy.json'
            policy.write_text(
                json.dumps(
                    {
                        'schema_version': 1,
                        'enforcement': 'network_default_deny',
                        'workloads': ['auth-server', 'backend', 'queue-worker'],
                        'denied_targets': [
                            '1.1.1.1',
                            'api.openai.com',
                            'api.omi.me',
                            'api.anthropic.com',
                            'generativelanguage.googleapis.com',
                        ],
                    }
                )
                + '\n',
                encoding='utf-8',
            )
            freeze_lease = root / 'source-freeze.json'
            SOURCE_FREEZE.issue_lease(
                freeze_lease,
                source_project='source-project',
                source_database='(default)',
                source_endpoint='https://firestore.googleapis.com',
                scopes=['firestore', 'storage'],
                holder='test-change',
                ttl_seconds=3600,
                secret='test-source-freeze-secret',
            )
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            call_log = root / 'docker.calls'
            docker = bin_dir / 'docker'
            docker.write_text(
                '#!/bin/sh\nprintf "%s|MLX_ENDPOINT=%s|MLX_MODEL=%s|SENSEVOICE_PATH=%s|SPEAKER_PATH=%s|TTS_PATH=%s|LLM_URL=%s\\n" '
                '"$*" "${MLX_MOSS_DIARIZE_ENDPOINT-unset}" "${MLX_MOSS_DIARIZE_MODEL-unset}" '
                '"${SENSEVOICE_MODEL_HOST_PATH-unset}" "${SPEAKER_MODEL_HOST_DIR-unset}" '
                '"${TTS_MODEL_HOST_DIR-unset}" "${GENERIC_OPENAI_BASE_URL-unset}" >> "$FAKE_DOCKER_CALLS"\n'
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
                'MLX_MOSS_DIARIZE_ENDPOINT': 'https://host-injected.example/v1/audio/transcriptions',
                'MLX_MOSS_DIARIZE_MODEL': 'host-injected-model',
                'SENSEVOICE_MODEL_HOST_PATH': '/host-injected/sensevoice',
                'SPEAKER_MODEL_HOST_DIR': '/host-injected/speaker',
                'TTS_MODEL_HOST_DIR': '/host-injected/tts',
                'GENERIC_OPENAI_BASE_URL': 'https://host-injected.example/v1',
                'SELF_HOST_SOURCE_WRITE_FREEZE_LEASE': str(freeze_lease),
                'SELF_HOST_SOURCE_PROJECT': 'source-project',
                'SELF_HOST_SOURCE_DATABASE': '(default)',
                'SELF_HOST_SOURCE_ENDPOINT': 'https://firestore.googleapis.com',
                'OMI_SOURCE_WRITE_FREEZE_SECRET': 'test-source-freeze-secret',
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
            for key in ('MLX_ENDPOINT', 'MLX_MODEL', 'SENSEVOICE_PATH', 'SPEAKER_PATH', 'TTS_PATH', 'LLM_URL'):
                self.assertGreaterEqual(calls.count(f'{key}=unset'), 5, key)
            self.assertNotIn('host-injected', calls)

            policy.write_text('{"schema_version":1,"enforcement":"network_default_deny"}\n', encoding='utf-8')
            invalid_policy = subprocess.run(
                ['bash', str(CUTOVER_GATE), '--external'],
                check=False,
                capture_output=True,
                text=True,
                env={**base_environment, 'SELF_HOST_EGRESS_POLICY_ARTIFACT': str(policy)},
            )
            self.assertEqual(invalid_policy.returncode, 1)
            self.assertIn('does not satisfy the reviewed JSON contract', invalid_policy.stderr)

            policy.write_text(
                json.dumps(
                    {
                        'schema_version': 1,
                        'enforcement': 'network_default_deny',
                        'workloads': ['auth-server', 'backend', 'queue-worker'],
                        'denied_targets': [
                            '1.1.1.1',
                            'api.openai.com',
                            'api.omi.me',
                            'api.anthropic.com',
                            'generativelanguage.googleapis.com',
                        ],
                    }
                )
                + '\n',
                encoding='utf-8',
            )

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

    def test_external_policy_contract_rejects_partial_or_unknown_scope(self) -> None:
        valid = {
            'schema_version': 1,
            'enforcement': 'network_default_deny',
            'workloads': ['auth-server', 'backend', 'queue-worker'],
            'denied_targets': [
                '1.1.1.1',
                'api.openai.com',
                'api.omi.me',
                'api.anthropic.com',
                'generativelanguage.googleapis.com',
            ],
        }
        self.assertEqual(EGRESS_POLICY.validate_policy(valid), valid)
        for field, value in (
            ('schema_version', True),
            ('workloads', ['backend']),
            ('denied_targets', valid['denied_targets'][:-1]),
            ('enforcement', 'document_only'),
        ):
            candidate = dict(valid)
            candidate[field] = value
            with self.assertRaisesRegex(ValueError, 'egress policy'):
                EGRESS_POLICY.validate_policy(candidate)
        unknown = {**valid, 'operator': 'ticket-123'}
        with self.assertRaisesRegex(ValueError, 'keys must be exactly'):
            EGRESS_POLICY.validate_policy(unknown)

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
            key_file = self._key_file(root)
            source = root / 'state'
            source.mkdir()
            (source / 'nested').mkdir()
            (source / 'nested' / 'record.json').write_text('{"version":1}\n', encoding='utf-8')
            archive = root / 'state.tar.gz.enc'
            fingerprints = ('a' * 64, 'b' * 64, 'c' * 64)

            SNAPSHOT.backup(source, archive, key_file)
            SNAPSHOT.write_manifest(root, 'deadbeef', [archive.name], *fingerprints)
            SNAPSHOT.verify_manifest(
                root,
                [archive.name],
                dict(zip(('runtime_fingerprint', 'config_fingerprint', 'migration_fingerprint'), fingerprints)),
                key_file,
            )
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((root / 'manifest.json').stat().st_mode), 0o600)
            self.assertTrue(archive.read_bytes().startswith(SNAPSHOT.ENVELOPE_MAGIC))
            self.assertNotIn(key_file.read_bytes(), (root / 'manifest.json').read_bytes())

            (source / 'nested' / 'record.json').write_text('corrupt', encoding='utf-8')
            (source / 'stale').write_text('must disappear', encoding='utf-8')
            SNAPSHOT.restore(source, archive, key_file)
            self.assertEqual((source / 'nested' / 'record.json').read_text(encoding='utf-8'), '{"version":1}\n')
            self.assertFalse((source / 'stale').exists())

            archive.write_bytes(archive.read_bytes() + b'tampered')
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                SNAPSHOT.verify_manifest(
                    root,
                    [archive.name],
                    dict(zip(('runtime_fingerprint', 'config_fingerprint', 'migration_fingerprint'), fingerprints)),
                    key_file,
                )

    def test_restore_rejects_archive_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = self._key_file(root)
            source = root / 'state'
            source.mkdir()
            plaintext = root / 'unsafe.tar.gz'
            with tarfile.open(plaintext, 'w:gz') as archive:
                member = tarfile.TarInfo('../outside')
                member.size = 1
                archive.addfile(member, io.BytesIO(b'x'))
            archive_path = root / 'unsafe.tar.gz.enc'
            SNAPSHOT.seal_file(plaintext, archive_path, key_file)
            plaintext.unlink()

            with self.assertRaisesRegex(RuntimeError, 'unsafe archive member'):
                SNAPSHOT.restore(source, archive_path, key_file)

    def test_backup_rejects_wrong_key_and_tamper_without_mutating_restore_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = self._key_file(root)
            wrong_key_file = root / 'wrong.key'
            wrong_key_file.write_bytes(b'w' * 32)
            wrong_key_file.chmod(0o600)
            source = root / 'state'
            source.mkdir()
            (source / 'marker').write_text('keep', encoding='utf-8')
            archive = root / 'state.tar.gz.enc'
            SNAPSHOT.backup(source, archive, key_file)

            with self.assertRaisesRegex(RuntimeError, 'authentication failed'):
                SNAPSHOT.restore(source, archive, wrong_key_file)
            self.assertEqual((source / 'marker').read_text(encoding='utf-8'), 'keep')

            tampered = root / 'tampered.tar.gz.enc'
            tampered.write_bytes(archive.read_bytes())
            tampered_bytes = bytearray(tampered.read_bytes())
            ciphertext_offset = (
                len(SNAPSHOT.ENVELOPE_MAGIC)
                + SNAPSHOT._HEADER.size
                + SNAPSHOT._LENGTH.size
                + SNAPSHOT.ENVELOPE_NONCE_BYTES
            )
            tampered_bytes[ciphertext_offset] ^= 1
            tampered.write_bytes(tampered_bytes)
            tampered.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'authentication failed'):
                SNAPSHOT.restore(source, tampered, key_file)
            self.assertEqual((source / 'marker').read_text(encoding='utf-8'), 'keep')

    def test_backup_key_requires_exact_private_permissions_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = self._key_file(root)
            source = root / 'state'
            source.mkdir()
            archive = root / 'state.tar.gz.enc'
            key_file.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'mode 0600'):
                SNAPSHOT.backup(source, archive, key_file)
            key_file.chmod(0o600)
            key_file.write_bytes(b'short')
            with self.assertRaisesRegex(RuntimeError, 'exactly 32 bytes'):
                SNAPSHOT.backup(source, archive, key_file)

    def test_manifest_records_source_revision_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / 'postgres.dump.enc'
            artifact.write_bytes(b'dump')
            SNAPSHOT.write_manifest(root, 'cafebabe', [artifact.name], 'a' * 64, 'b' * 64, 'c' * 64)

            payload = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(payload['schema_version'], 3)
            self.assertEqual(payload['git_sha'], 'cafebabe')
            self.assertEqual(payload['runtime_fingerprint'], 'a' * 64)
            self.assertEqual(payload['config_fingerprint'], 'b' * 64)
            self.assertEqual(payload['migration_fingerprint'], 'c' * 64)
            self.assertEqual(set(payload['artifacts']), {'postgres.dump.enc'})
            self.assertEqual(payload['encryption']['format'], SNAPSHOT.ENVELOPE_FORMAT)
            self.assertNotIn('secret', json.dumps(payload).lower())

    def test_schema_v3_manifest_cli_binds_all_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = self._key_file(root)
            plaintext = root / 'postgres.dump'
            plaintext.write_bytes(b'dump')
            artifact = root / 'postgres.dump.enc'
            SNAPSHOT.seal_file(plaintext, artifact, key_file)
            plaintext.unlink()
            fingerprints = {
                'runtime_fingerprint': 'a' * 64,
                'config_fingerprint': 'b' * 64,
                'migration_fingerprint': 'c' * 64,
            }
            manifest = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    'manifest',
                    str(root),
                    '--git-sha',
                    'cafebabe',
                    '--runtime-fingerprint',
                    fingerprints['runtime_fingerprint'],
                    '--config-fingerprint',
                    fingerprints['config_fingerprint'],
                    '--migration-fingerprint',
                    fingerprints['migration_fingerprint'],
                    artifact.name,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(manifest.returncode, 0, manifest.stderr)

            verified = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    'verify',
                    str(root),
                    '--expected-files',
                    artifact.name,
                    '--expected-git-sha',
                    'cafebabe',
                    '--expected-runtime-fingerprint',
                    fingerprints['runtime_fingerprint'],
                    '--expected-config-fingerprint',
                    fingerprints['config_fingerprint'],
                    '--expected-migration-fingerprint',
                    fingerprints['migration_fingerprint'],
                    '--key-file',
                    str(key_file),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)

            payload = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
            with self.assertRaisesRegex(RuntimeError, 'backup git_sha does not match'):
                SNAPSHOT.verify_manifest(root, [artifact.name], fingerprints, key_file, 'different-current-revision')
            payload['migration_fingerprint'] = 'd' * 64
            (root / 'manifest.json').write_text(json.dumps(payload), encoding='utf-8')
            (root / 'manifest.json').chmod(0o600)
            rejected = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    'verify',
                    str(root),
                    '--expected-files',
                    artifact.name,
                    '--expected-git-sha',
                    'cafebabe',
                    '--expected-runtime-fingerprint',
                    fingerprints['runtime_fingerprint'],
                    '--expected-config-fingerprint',
                    fingerprints['config_fingerprint'],
                    '--expected-migration-fingerprint',
                    fingerprints['migration_fingerprint'],
                    '--key-file',
                    str(key_file),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn('backup migration_fingerprint does not match', rejected.stderr)

    def test_manifest_rejects_structurally_incomplete_v3_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = self._key_file(root)
            manifest_path = root / 'manifest.json'
            manifest_path.write_text('[]', encoding='utf-8')
            manifest_path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'unsupported backup manifest'):
                SNAPSHOT.verify_manifest(root, key_file=key_file)

            artifact = root / 'postgres.dump.enc'
            artifact.write_bytes(b'dump')
            SNAPSHOT.write_manifest(root, 'cafebabe', [artifact.name], 'a' * 64, 'b' * 64, 'c' * 64)
            payload = json.loads(manifest_path.read_text(encoding='utf-8'))
            del payload['git_sha']
            manifest_path.write_text(json.dumps(payload), encoding='utf-8')
            manifest_path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'git_sha'):
                SNAPSHOT.verify_manifest(root, key_file=key_file)

    def test_manifest_verification_fails_closed_for_missing_or_tampered_fingerprints_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = self._key_file(root)
            artifact = root / 'postgres.dump.enc'
            artifact.write_bytes(b'dump')
            fingerprints = {
                'runtime_fingerprint': 'a' * 64,
                'config_fingerprint': 'b' * 64,
                'migration_fingerprint': 'c' * 64,
            }
            SNAPSHOT.write_manifest(root, 'cafebabe', [artifact.name], **fingerprints)
            manifest_path = root / 'manifest.json'
            payload = json.loads(manifest_path.read_text(encoding='utf-8'))
            del payload['migration_fingerprint']
            manifest_path.write_text(json.dumps(payload), encoding='utf-8')
            manifest_path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'migration_fingerprint'):
                SNAPSHOT.verify_manifest(root, [artifact.name], fingerprints, key_file)

            payload['migration_fingerprint'] = 'not-a-fingerprint'
            manifest_path.write_text(json.dumps(payload), encoding='utf-8')
            manifest_path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'migration_fingerprint'):
                SNAPSHOT.verify_manifest(root, [artifact.name], fingerprints, key_file)

            payload['migration_fingerprint'] = fingerprints['migration_fingerprint']
            manifest_path.write_text(json.dumps(payload), encoding='utf-8')
            manifest_path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'mode 0600'):
                SNAPSHOT.verify_manifest(root, [artifact.name], fingerprints, key_file)

            manifest_path.chmod(0o600)
            artifact.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, 'mode 0600'):
                SNAPSHOT.verify_manifest(root, [artifact.name], fingerprints, key_file)

    def test_manifest_writer_rejects_symlink_without_overwriting_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'outside.json'
            target.write_text('keep me\n', encoding='utf-8')
            manifest_path = root / 'manifest.json'
            manifest_path.symlink_to(target)
            artifact = root / 'postgres.dump.enc'
            artifact.write_bytes(b'ciphertext')

            with self.assertRaisesRegex(RuntimeError, 'manifest must not be a symlink'):
                SNAPSHOT.write_manifest(root, 'cafebabe', [artifact.name], 'a' * 64, 'b' * 64, 'c' * 64)
            self.assertEqual(target.read_text(encoding='utf-8'), 'keep me\n')

    def test_manifest_writer_rejects_non_regular_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / 'postgres.dump.enc'
            artifact.write_bytes(b'ciphertext')
            manifest_path = root / 'manifest.json'
            manifest_path.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'manifest must be a regular file'):
                SNAPSHOT.write_manifest(root, 'cafebabe', [artifact.name], 'a' * 64, 'b' * 64, 'c' * 64)

    def test_backup_restore_contract_requires_key_and_documents_recovery_drill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'state'
            source.mkdir()
            archive = root / 'state.tar.gz.enc'
            no_key_commands = (
                ['backup', str(source), str(archive)],
                ['restore', str(source), str(archive)],
                ['seal', str(source), str(archive)],
                ['seal-stdin', str(archive)],
                ['open', str(archive), str(root / 'plaintext')],
                [
                    'verify',
                    str(root),
                    '--expected-git-sha',
                    'cafebabe',
                    '--expected-runtime-fingerprint',
                    'a' * 64,
                    '--expected-config-fingerprint',
                    'b' * 64,
                    '--expected-migration-fingerprint',
                    'c' * 64,
                ],
            )
            for command in no_key_commands:
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), *command],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2, command)
                self.assertIn('--key-file', result.stderr, command)

        readme = SCRIPT.with_name('README.md').read_text(encoding='utf-8')
        for required in (
            'operations.sh backup',
            'operations.sh verify-backup',
            'SELF_HOST_RESTORE_ACK=I_ACKNOWLEDGE_THIS_OVERWRITES_STATE',
            'operations.sh restore',
            'make self-host-migration-gate',
            'completed restore drill',
            'isolated restore host',
            'Qdrant projection',
            'Typesense projection',
            'external evidence',
            'SELF_HOST_RECOVERY_EVIDENCE',
            'production_kms_attested',
        ):
            self.assertIn(required, readme)

    def test_operations_bind_all_backup_fingerprints_and_expected_artifacts(self) -> None:
        script = OPERATIONS.read_text(encoding='utf-8')
        self.assertIn('runtime_fingerprint()', script)
        self.assertIn('migration_fingerprint()', script)
        self.assertIn('--runtime-fingerprint "$runtime_sha256"', script)
        self.assertIn('--config-fingerprint "$config_sha256"', script)
        self.assertIn('--migration-fingerprint "$migration_sha256"', script)
        self.assertIn('--expected-git-sha "$git_sha"', script)
        self.assertIn('--key-file /backup-key/key', script)
        self.assertIn('postgres.dump.enc', script)
        self.assertIn('verify /backup \\\n      --expected-files "${ARCHIVE_FILES[@]}"', script)

    def test_restore_and_start_static_contract_is_fail_closed(self) -> None:
        """Tripwire for the destructive ordering; live Compose proves behavior."""

        script = OPERATIONS.read_text(encoding='utf-8')
        recreate = script.index('dropdb -U "$POSTGRES_USER" --force --if-exists')
        restore = script.index("pg_restore -U \"$POSTGRES_USER\"")
        migration = script.index('compose run --rm --no-deps -T auth-migrate')
        firestore_migration = script.index('compose run --rm --no-deps -T firestore-pg-migrate')
        application_start = script.index('compose up --detach --wait --no-deps "${APPLICATION_SERVICES[@]}"')
        self.assertLess(recreate, restore)
        self.assertLess(migration, firestore_migration)
        self.assertLess(firestore_migration, application_start)


if __name__ == '__main__':
    unittest.main()
