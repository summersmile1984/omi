#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import tempfile
import unittest
import shutil
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('check_self_host_deployment.py')
SPEC = importlib.util.spec_from_file_location('check_self_host_deployment', SCRIPT)
assert SPEC and SPEC.loader
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class SelfHostDeploymentContractTest(unittest.TestCase):
    def validate_mutation(
        self,
        *,
        compose_replace: tuple[str, str] | None = None,
        env_replace: tuple[str, str] | None = None,
        env_append: str = '',
    ) -> list[str]:
        compose = CHECK.DEFAULT_COMPOSE.read_text(encoding='utf-8')
        if compose_replace:
            compose = compose.replace(*compose_replace, 1)
        env = CHECK.DEFAULT_EXAMPLE_ENV.read_text(encoding='utf-8')
        if env_replace:
            env = env.replace(*env_replace, 1)
        env += env_append
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compose_path = root / 'compose.yml'
            env_path = root / '.env'
            compose_path.write_text(compose, encoding='utf-8')
            env_path.write_text(env, encoding='utf-8')
            return CHECK.validate(compose_path, env_path)

    def test_checked_in_profile_is_complete_and_zero_vendor(self) -> None:
        self.assertEqual(CHECK.validate(CHECK.DEFAULT_COMPOSE, CHECK.DEFAULT_EXAMPLE_ENV), [])

    def test_runtime_env_rejects_example_placeholders_and_missing_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / '.env.production'
            env_path.write_text(CHECK.DEFAULT_EXAMPLE_ENV.read_text(encoding='utf-8'), encoding='utf-8')
            errors = CHECK.validate(CHECK.DEFAULT_COMPOSE, env_path)

        self.assertTrue(any('unreplaced placeholders' in error for error in errors))
        self.assertIn('PUBLIC_AUTH_URL must not use the reserved example.com deployment host', errors)
        self.assertIn('SENSEVOICE_MODEL_HOST_PATH must be an existing absolute directory', errors)

    def test_macos_client_model_egress_requires_pre_transport_guards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in CHECK.MACOS_MODEL_BOUNDARY_REQUIREMENTS:
                source = CHECK.ROOT / relative
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)

            chat_lab = root / 'desktop/macos/Desktop/Sources/MainWindow/Pages/ChatLabView.swift'
            chat_lab.write_text(
                chat_lab.read_text(encoding='utf-8').replace(
                    'allowsClientDirectVendorEgress',
                    'unguardedVendorEgress',
                ),
                encoding='utf-8',
            )

            errors = CHECK.validate_macos_client_model_egress(root)
            self.assertTrue(any('ChatLabView.swift missing self-hosted model boundary' in error for error in errors))

            runtime_egress = root / 'desktop/macos/Desktop/Sources/Chat/AgentRuntimeEgressPolicy.swift'
            runtime_egress.write_text(
                runtime_egress.read_text(encoding='utf-8').replace(
                    'allowsAgentAdapter(',
                    'allowsUnreviewedAdapter(',
                ),
                encoding='utf-8',
            )
            errors = CHECK.validate_macos_client_model_egress(root)
            self.assertTrue(
                any('AgentRuntimeEgressPolicy.swift missing self-hosted model boundary' in error for error in errors)
            )

    def test_windows_and_flutter_release_paths_require_pre_transport_guards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = {
                **CHECK.WINDOWS_MODEL_BOUNDARY_REQUIREMENTS,
                **CHECK.FLUTTER_MODEL_BOUNDARY_REQUIREMENTS,
            }
            for relative in requirements:
                source = CHECK.ROOT / relative
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            env_source = CHECK.ROOT / 'desktop/windows/.env.selfhost.example'
            env_target = root / 'desktop/windows/.env.selfhost.example'
            env_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(env_source, env_target)

            voice = root / 'desktop/windows/src/renderer/src/lib/voice/voiceController.ts'
            voice.write_text(
                voice.read_text(encoding='utf-8').replace(
                    'resolveWindowsDeployment().allowDirectModelProviders',
                    'unguardedDirectProvider',
                ),
                encoding='utf-8',
            )
            errors = CHECK.validate_release_client_model_egress(root)
            self.assertTrue(any('voiceController.ts missing self-hosted model boundary' in error for error in errors))

            transcription = root / 'app/lib/services/sockets/transcription_service.dart'
            transcription.write_text(
                transcription.read_text(encoding='utf-8').replace(
                    'createTransportForProfile',
                    'constructTransportWithoutProfile',
                ),
                encoding='utf-8',
            )
            errors = CHECK.validate_release_client_model_egress(root)
            self.assertTrue(any('transcription_service.dart missing self-hosted model boundary' in error for error in errors))

    def test_windows_self_host_example_rejects_firebase_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in {
                **CHECK.WINDOWS_MODEL_BOUNDARY_REQUIREMENTS,
                **CHECK.FLUTTER_MODEL_BOUNDARY_REQUIREMENTS,
            }:
                source = CHECK.ROOT / relative
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            env_target = root / 'desktop/windows/.env.selfhost.example'
            env_target.parent.mkdir(parents=True, exist_ok=True)
            env_target.write_text('VITE_FIREBASE_API_KEY=forbidden\n', encoding='utf-8')
            errors = CHECK.validate_release_client_model_egress(root)
            self.assertIn('Windows self-host example must not declare Firebase configuration', errors)

    def test_rejects_http_public_auth_origin(self) -> None:
        errors = self.validate_mutation(
            env_append='\nPUBLIC_AUTH_URL=http://auth.example.com\n',
        )
        self.assertIn('PUBLIC_AUTH_URL must be an explicit https URL', errors)

    def test_private_auth_control_plane_is_explicit_and_cannot_fall_back_public(self) -> None:
        errors = self.validate_mutation(
            compose_replace=(
                'AUTH_JWKS_URL=http://auth-server:3000/api/auth/jwks',
                'AUTH_JWKS_URL=http://auth.example.com/api/auth/jwks',
            )
        )
        self.assertIn('backend AUTH_JWKS_URL must use the private auth-server service endpoint', errors)

        errors = self.validate_mutation(
            compose_replace=('AUTH_INTERNAL_ALLOW_HTTP=true', 'AUTH_INTERNAL_ALLOW_HTTP=false')
        )
        self.assertIn("backend AUTH_INTERNAL_ALLOW_HTTP must be literal 'true'", errors)

    def test_rejects_official_provider_binding_and_endpoint(self) -> None:
        errors = self.validate_mutation(
            env_append='\nOPENAI_API_KEY=not-allowed\nGENERIC_OPENAI_BASE_URL=https://api.openai.com/v1\n',
        )
        self.assertTrue(any('OPENAI_API_KEY' in error for error in errors))
        self.assertTrue(any('api.openai.com' in error for error in errors))

    def test_rejects_missing_healthcheck_and_optionalized_secret(self) -> None:
        errors = self.validate_mutation(
            compose_replace=(
                '    healthcheck:\n      test: ["CMD", "redis-cli", "ping"]',
                '    x-healthcheck-removed:\n      test: ["CMD", "redis-cli", "ping"]',
            )
        )
        self.assertIn('redis must define a healthcheck', errors)

        errors = self.validate_mutation(
            compose_replace=(
                'GENERIC_OPENAI_API_KEY=${GENERIC_OPENAI_API_KEY:?GENERIC_OPENAI_API_KEY is required}',
                'GENERIC_OPENAI_API_KEY=${GENERIC_OPENAI_API_KEY:-}',
            )
        )
        self.assertIn('backend GENERIC_OPENAI_API_KEY must use required ${VAR:?message} interpolation', errors)

    def test_rejects_duplicate_service_environment(self) -> None:
        errors = self.validate_mutation(
            compose_replace=(
                '      - ADMIN_KEY_AUTH_ENABLED=false\n',
                '      - ADMIN_KEY_AUTH_ENABLED=false\n      - ADMIN_KEY_AUTH_ENABLED=false\n',
            )
        )
        self.assertIn('backend contains duplicate environment: ADMIN_KEY_AUTH_ENABLED', errors)

    def test_rejects_default_searxng_secret_and_unbounded_engines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / 'settings.yml'
            settings.write_text(
                'use_default_settings: true\nserver:\n  secret_key: "ultrasecretkey"\n'
                'search:\n  formats:\n    - json\n',
                encoding='utf-8',
            )
            with patch.object(CHECK, 'DEFAULT_SEARXNG_SETTINGS', settings):
                errors = CHECK.validate(CHECK.DEFAULT_COMPOSE, CHECK.DEFAULT_EXAMPLE_ENV)

        self.assertIn('SearXNG settings must receive its secret from required SEARXNG_SECRET injection', errors)
        self.assertIn('SearXNG outbound engine allowlist must keep only wikipedia', errors)

    def test_rejects_auth_start_without_explicit_successful_migration(self) -> None:
        errors = self.validate_mutation(
            compose_replace=(
                '    command: ["node", "src/migrate.js"]',
                '    command: ["node", "src/index.js"]',
            )
        )
        self.assertIn('auth-migrate must run the explicit Better Auth schema migrator', errors)

        errors = self.validate_mutation(
            compose_replace=(
                '        condition: service_completed_successfully',
                '        condition: service_started',
            )
        )
        self.assertIn('auth-server must fail closed behind successful auth-migrate completion', errors)

    def test_rejects_mutable_state_service_image_tag(self) -> None:
        errors = self.validate_mutation(
            compose_replace=(
                'postgres:16.4-alpine@sha256:5660c2cbfea50c7a9127d17dc4e48543eedd3d7a41a595a2dfa572471e37e64c',
                'postgres:16.4-alpine',
            )
        )
        self.assertIn('postgres image must be pinned by sha256 digest', errors)

    def test_rejects_jwks_grace_shorter_than_issued_token_lifetime(self) -> None:
        errors = self.validate_mutation(env_append='\nAUTH_JWKS_GRACE_SECONDS=60\n')
        self.assertIn('AUTH_JWKS_GRACE_SECONDS must be at least the 15 minute JWT lifetime', errors)

    def test_requires_explicit_realtime_relay_and_projection_bindings(self) -> None:
        errors = self.validate_mutation(
            compose_replace=(
                '      - REALTIME_PROVIDER=relay\n',
                '',
            )
        )
        self.assertIn("backend REALTIME_PROVIDER must be literal 'relay'", errors)

        errors = self.validate_mutation(
            compose_replace=(
                '${VECTOR_PROJECTION_ACTIVE_VERSION:?VECTOR_PROJECTION_ACTIVE_VERSION is required}',
                '${VECTOR_PROJECTION_ACTIVE_VERSION:-v1}',
            )
        )
        self.assertTrue(
            any('VECTOR_PROJECTION_ACTIVE_VERSION must use exact projection binding' in error for error in errors)
        )

    def test_realtime_relay_requires_an_allowlisted_non_vendor_target(self) -> None:
        errors = self.validate_mutation(
            env_replace=(
                'REALTIME_RELAY_URL=wss://realtime.example.com/v1/realtime',
                'REALTIME_RELAY_URL=wss://api.openai.com/v1/realtime',
            )
        )
        self.assertTrue(any('official endpoint host api.openai.com' in error for error in errors))
        self.assertIn('REALTIME_RELAY_ALLOWED_HOSTS must contain the exact REALTIME_RELAY_URL host', errors)

        errors = self.validate_mutation(
            env_replace=(
                'REALTIME_RELAY_URL=wss://realtime.example.com/v1/realtime',
                'REALTIME_RELAY_URL=ws://realtime.example.com/v1/realtime',
            )
        )
        self.assertIn('REALTIME_RELAY_URL must use wss for a public target host', errors)

        errors = self.validate_mutation(
            env_append=(
                '\nREALTIME_RELAY_URL=ws://169.254.169.254/v1/realtime'
                '\nREALTIME_RELAY_ALLOWED_HOSTS=169.254.169.254\n'
            )
        )
        self.assertIn('REALTIME_RELAY_URL must not target link-local, metadata, or reserved hosts', errors)

        errors = self.validate_mutation(
            env_replace=(
                'REALTIME_RELAY_ALLOWED_HOSTS=realtime.example.com',
                'REALTIME_RELAY_ALLOWED_HOSTS=other.example.com',
            )
        )
        self.assertIn('REALTIME_RELAY_ALLOWED_HOSTS must contain the exact REALTIME_RELAY_URL host', errors)

        errors = self.validate_mutation(
            compose_replace=('MEMORY_KEYWORD_INDEX_PROVIDER=disabled', 'MEMORY_KEYWORD_INDEX_PROVIDER=typesense')
        )
        self.assertIn("backend MEMORY_KEYWORD_INDEX_PROVIDER must be literal 'disabled'", errors)

        errors = self.validate_mutation(
            compose_replace=('SPEAKER_EMBEDDING_PROVIDER=disabled', 'SPEAKER_EMBEDDING_PROVIDER=hosted')
        )
        self.assertIn("backend SPEAKER_EMBEDDING_PROVIDER must be literal 'disabled'", errors)

        errors = self.validate_mutation(compose_replace=('TTS_PROVIDER=disabled', 'TTS_PROVIDER=openai'))
        self.assertIn("backend TTS_PROVIDER must be literal 'disabled'", errors)

        errors = self.validate_mutation(
            env_replace=(
                'REALTIME_RELAY_WIRE_PROTOCOL=openai_realtime_v1',
                'REALTIME_RELAY_WIRE_PROTOCOL=gemini_live_v1',
            )
        )
        self.assertIn('REALTIME_RELAY_WIRE_PROTOCOL must be openai_realtime_v1', errors)

        errors = self.validate_mutation(
            compose_replace=('WEB_SEARCH_TRANSPORT=searxng', 'WEB_SEARCH_TRANSPORT=disabled')
        )
        self.assertIn("backend WEB_SEARCH_TRANSPORT must be literal 'searxng'", errors)

    def test_rejects_incoherent_projection_version_state(self) -> None:
        errors = self.validate_mutation(
            env_append=(
                '\nVECTOR_PROJECTION_MODE=dual_write\n'
                'VECTOR_PROJECTION_ACTIVE_VERSION=v1\n'
                'VECTOR_PROJECTION_TARGET_VERSION=v1\n'
                'VECTOR_PROJECTION_SCHEMA_VERSION=0\n'
                'VECTOR_PROJECTION_DELETE_VERSIONS=v2\n'
            )
        )
        self.assertIn('dual_write requires a distinct VECTOR_PROJECTION_TARGET_VERSION', errors)
        self.assertIn('VECTOR_PROJECTION_SCHEMA_VERSION must be a positive integer', errors)
        self.assertIn('VECTOR_PROJECTION_DELETE_VERSIONS must retain every active/target version', errors)

    def test_requires_blank_target_declaration_in_single_mode(self) -> None:
        errors = self.validate_mutation(
            env_replace=(
                'VECTOR_PROJECTION_TARGET_VERSION=\n',
                '',
            )
        )
        self.assertIn(
            '.env must declare VECTOR_PROJECTION_TARGET_VERSION, blank in single mode',
            errors,
        )

    def test_requires_every_global_projection_namespace(self) -> None:
        errors = self.validate_mutation(env_append='\nVECTOR_PROJECTION_REQUIRED_NAMESPACES=ns1,ns2\n')
        self.assertIn(
            'VECTOR_PROJECTION_REQUIRED_NAMESPACES must cover every self-host vector namespace',
            errors,
        )


if __name__ == '__main__':
    unittest.main()
