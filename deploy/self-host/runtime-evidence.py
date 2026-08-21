#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Collect fail-closed health and source/config identity from exact Compose workloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REQUIRED_SERVICES = (
    'postgres',
    'redis',
    'minio',
    'qdrant',
    'typesense',
    'searxng',
    'auth-server',
    'backend',
    'queue-worker',
)
SOURCE_WORKLOADS = ('auth-server', 'backend', 'queue-worker')
OBJECT_ID = re.compile(r'^[0-9a-f]{40}$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')
IMAGE_ID = re.compile(r'^sha256:[0-9a-f]{64}$')
COMPOSE_WRAPPER = Path(__file__).with_name('compose-clean-env.sh')
IDENTIFIER = re.compile(r'^[a-z0-9][a-z0-9_.:-]*$')
HOSTNAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.-]*$')


def _required_text(environment: dict[str, Any], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str):
        raise ValueError(f'effective provider configuration is missing {name}')
    value = value.strip()
    if not value:
        raise ValueError(f'effective provider configuration is missing {name}')
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f'{name} contains an invalid control character')
    return value


def _parsed_safe_endpoint(environment: dict[str, Any], name: str, *, schemes: set[str]) -> tuple[str, Any]:
    value = _required_text(environment, name)
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing .port validates the port syntax and range.
        parsed.port
    except ValueError as error:
        raise ValueError(f'{name} must be a credential-free endpoint') from error
    if (
        parsed.scheme not in schemes
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in parsed.netloc)
    ):
        raise ValueError(f'{name} must be a credential-free endpoint without query or fragment')
    return value, parsed


def _safe_endpoint_origin(environment: dict[str, Any], name: str, *, schemes: set[str]) -> str:
    _, parsed = _parsed_safe_endpoint(environment, name, schemes=schemes)
    # Keep only the authority in the evidence.  Paths may identify a protocol
    # route, but are not part of the provider identity and may contain tenant
    # details in operator-managed URLs.
    return f'{parsed.scheme}://{parsed.netloc}'


def _safe_endpoint(
    environment: dict[str, Any], name: str, *, schemes: set[str], required_path: str | None = None
) -> str:
    value, parsed = _parsed_safe_endpoint(environment, name, schemes=schemes)
    if required_path is not None and parsed.path != required_path:
        raise ValueError(f'{name} must use the exact {required_path} path')
    return value


def _safe_model_name(environment: dict[str, Any], name: str) -> str:
    value = _required_text(environment, name)
    if any(character in value for character in '\r\n\x00'):
        raise ValueError(f'{name} contains an invalid control character')
    return value


def _basename_model(environment: dict[str, Any], name: str) -> str:
    value = _safe_model_name(environment, name)
    basename = Path(value).name
    if basename in {'', '.', '..'}:
        raise ValueError(f'{name} does not identify a model artifact')
    return basename


def _safe_identifier(environment: dict[str, Any], name: str) -> str:
    value = _required_text(environment, name)
    if not IDENTIFIER.fullmatch(value.lower()):
        raise ValueError(f'{name} must be a safe provider identifier')
    return value


def _safe_host(environment: dict[str, Any], name: str) -> str:
    value = _required_text(environment, name)
    if not HOSTNAME.fullmatch(value):
        raise ValueError(f'{name} must be a host name without credentials or a URL')
    return value


def _positive_integer(environment: dict[str, Any], name: str) -> str:
    value = _required_text(environment, name)
    if not value.isdecimal() or int(value) <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


def environment_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_effective_config_sha256(config: dict[str, Any]) -> str:
    """Hash effective Compose behavior without its self-referential hash label."""
    normalized = json.loads(json.dumps(config))
    services = normalized.get('services') if isinstance(normalized.get('services'), dict) else {}
    for service in SOURCE_WORKLOADS:
        row = services.get(service) if isinstance(services.get(service), dict) else {}
        labels = row.get('labels') if isinstance(row.get('labels'), dict) else {}
        labels.pop('com.omi.runtime.config-sha256', None)
    encoded = json.dumps(normalized, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def load_effective_compose_config(*, compose_file: Path, env_file: Path) -> dict[str, Any]:
    environment = dict(os.environ)
    environment['OMI_RUNTIME_CONFIG_SHA256'] = '0' * 64
    result = subprocess.run(
        [
            'bash',
            str(COMPOSE_WRAPPER),
            str(env_file),
            str(compose_file),
            'config',
            '--format',
            'json',
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError(f'docker compose effective config failed: {result.stderr.strip()}')
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError('docker compose effective config was not an object')
    return value


def effective_compose_config_sha256(*, compose_file: Path, env_file: Path) -> str:
    return canonical_effective_config_sha256(
        load_effective_compose_config(compose_file=compose_file, env_file=env_file)
    )


PROVIDER_CONFIGURATION_KEYS = frozenset(
    {
        'stt_prerecorded_model',
        'mlx_moss_diarize_endpoint',
        'mlx_moss_diarize_model',
        'generic_llm_provider',
        'generic_llm_model',
        'generic_llm_transport',
        'generic_llm_endpoint_origin',
        'embedding_provider',
        'embedding_model',
        'embedding_transport',
        'embedding_dimension',
        'realtime_provider',
        'realtime_model',
        'realtime_transport',
        'realtime_endpoint_origin',
        'realtime_wire_protocol',
        'tts_provider',
        'tts_model',
        'tts_transport',
        'tts_endpoint_origin',
        'file_chat_provider',
        'file_chat_model',
        'file_chat_transport',
        'push_provider',
        'push_model',
        'push_transport',
        'memory_keyword_provider',
        'conversation_keyword_provider',
        'typesense_transport',
        'typesense_host',
        'memory_typesense_collection',
        'conversation_typesense_collection',
    }
)


def _validate_origin_value(value: Any, name: str, *, schemes: set[str]) -> None:
    if not isinstance(value, str):
        raise ValueError(f'{name} must be a sanitized endpoint origin')
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as error:
        raise ValueError(f'{name} must be a sanitized endpoint origin') from error
    if (
        parsed.scheme not in schemes
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {'', '/'}
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in parsed.netloc)
    ):
        raise ValueError(f'{name} must be a sanitized endpoint origin without userinfo or query')


def _validate_provider_configuration(configuration: dict[str, Any]) -> None:
    if not isinstance(configuration, dict):
        raise ValueError('effective provider configuration must be an object')
    if set(configuration) != PROVIDER_CONFIGURATION_KEYS:
        raise ValueError('effective provider configuration has an incomplete or unexpected identity shape')
    if any(str(key).lower().endswith(('_key', '_secret', '_token', '_password')) for key in configuration):
        raise ValueError('effective provider configuration must not contain credentials')
    for key, value in configuration.items():
        if key == 'tts_endpoint_origin' and value == '':
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'effective provider configuration is missing {key}')
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError(f'{key} contains an invalid control character')
    if configuration['stt_prerecorded_model'] != 'mlx_moss_diarize':
        raise ValueError('effective provider configuration selected the wrong prerecorded provider')
    _safe_endpoint(
        {'value': configuration['mlx_moss_diarize_endpoint']},
        'value',
        schemes={'http', 'https'},
        required_path='/v1/audio/transcriptions',
    )
    if (
        configuration['generic_llm_provider'] != 'generic'
        or configuration['generic_llm_transport'] != 'openai_compatible_http'
    ):
        raise ValueError('self-host generic LLM must use the operator-compatible HTTP provider')
    _validate_origin_value(
        configuration['generic_llm_endpoint_origin'], 'generic_llm_endpoint_origin', schemes={'http', 'https'}
    )
    if configuration['embedding_provider'] != 'generic' or configuration['embedding_transport'] != 'direct':
        raise ValueError('self-host embeddings must use the direct generic provider')
    if configuration['embedding_dimension'].isdecimal() is not True or int(configuration['embedding_dimension']) <= 0:
        raise ValueError('embedding_dimension must be a positive integer')
    if configuration['realtime_provider'] != 'relay' or configuration['realtime_transport'] != 'websocket_relay':
        raise ValueError('self-host realtime must use the bounded WebSocket relay')
    _validate_origin_value(configuration['realtime_endpoint_origin'], 'realtime_endpoint_origin', schemes={'ws', 'wss'})
    if configuration['realtime_wire_protocol'] != 'openai_realtime_v1':
        raise ValueError('self-host realtime uses an unsupported relay wire protocol')
    tts_origin = configuration['tts_endpoint_origin']
    if configuration['tts_provider'] == 'sherpa_onnx':
        if tts_origin:
            raise ValueError('local sherpa_onnx TTS must not include a remote endpoint origin')
    elif configuration['tts_provider'] == 'openai_compatible':
        if configuration['tts_transport'] != 'openai_compatible_http' or not tts_origin:
            raise ValueError('compatible TTS requires a credential-free endpoint origin')
        _validate_origin_value(tts_origin, 'tts_endpoint_origin', schemes={'http', 'https'})
    else:
        raise ValueError('self-host selected an unsupported TTS provider')
    if configuration['tts_provider'] == 'sherpa_onnx' and configuration['tts_transport'] != 'local':
        raise ValueError('local sherpa_onnx TTS must use the local transport')
    if (
        configuration['file_chat_provider'] != 'local_extraction'
        or configuration['file_chat_transport'] != 'local_extraction'
    ):
        raise ValueError('self-host file chat must use local extraction')
    if configuration['memory_keyword_provider'] != 'typesense':
        raise ValueError('memory keyword provider must be typesense')
    if configuration['conversation_keyword_provider'] != 'typesense':
        raise ValueError('conversation keyword provider must be typesense')
    if configuration['typesense_transport'] != 'http':
        raise ValueError('self-host Typesense must use HTTP transport')
    if not HOSTNAME.fullmatch(configuration['typesense_host']):
        raise ValueError('typesense_host must be a host name without credentials or a URL')
    if configuration['push_provider'] != 'disabled':
        raise ValueError('self-host push provider must be disabled')
    if configuration['push_model'] != 'disabled' or configuration['push_transport'] != 'disabled':
        raise ValueError('self-host push must be disabled')


def validate_runtime_snapshot(
    *,
    services: dict[str, dict[str, Any]],
    expected_git_commit: str,
    expected_git_tree: str,
    expected_config_sha256: str,
    effective_provider_configuration: dict[str, Any],
) -> dict[str, Any]:
    if not OBJECT_ID.fullmatch(expected_git_commit) or not OBJECT_ID.fullmatch(expected_git_tree):
        raise ValueError('expected source commit/tree must be full Git object IDs')
    if not SHA256.fullmatch(expected_config_sha256):
        raise ValueError('expected runtime config hash must be a sha256 digest')
    _validate_provider_configuration(effective_provider_configuration)

    missing = sorted(set(REQUIRED_SERVICES) - services.keys())
    if missing:
        raise ValueError(f'missing required running services: {", ".join(missing)}')

    health: dict[str, dict[str, Any]] = {}
    workloads: dict[str, dict[str, Any]] = {}
    for service in REQUIRED_SERVICES:
        row = services[service]
        state = str(row.get('state') or '')
        service_health = str(row.get('health') or '')
        health[service] = {'state': state, 'health': service_health}
        if state != 'running' or service_health != 'healthy':
            raise ValueError(f'{service} is not running and healthy: {state}/{service_health}')
        if service not in SOURCE_WORKLOADS:
            continue
        image_id = str(row.get('image_id') or '')
        source_commit = str(row.get('source_git_commit') or '')
        source_tree = str(row.get('source_git_tree') or '')
        config_sha256 = str(row.get('runtime_config_sha256') or '')
        if not IMAGE_ID.fullmatch(image_id):
            raise ValueError(f'{service} image ID is not content-addressed')
        if source_commit != expected_git_commit or source_tree != expected_git_tree:
            raise ValueError(f'{service} image source identity does not match the tested Git tree')
        if config_sha256 != expected_config_sha256:
            raise ValueError(f'{service} runtime config identity does not match the reviewed environment')
        if row.get('environment_matches_effective_config') is not True:
            raise ValueError(f'{service} runtime environment does not match effective reviewed Compose config')
        workloads[service] = {
            'image_id': image_id,
            'source_git_commit': source_commit,
            'source_git_tree': source_tree,
            'runtime_config_sha256': config_sha256,
            'environment_matches_effective_config': True,
        }

    return {
        'status': 'passed',
        'all_required_services_healthy': True,
        'service_health': health,
        'runtime_identity': {
            'expected_git_commit': expected_git_commit,
            'expected_git_tree': expected_git_tree,
            'expected_config_sha256': expected_config_sha256,
            'effective_provider_configuration': effective_provider_configuration,
            'source_and_config_match': True,
            'workloads': workloads,
        },
    }


def _run(command: list[str], *, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(command, check=False, capture_output=True, text=True, env=environment)
    if result.returncode != 0:
        raise RuntimeError(f'{" ".join(command[:3])} failed: {result.stderr.strip()}')
    return result.stdout.strip()


def effective_provider_configuration(effective: dict[str, Any]) -> dict[str, Any]:
    services = effective.get('services') if isinstance(effective.get('services'), dict) else {}
    backend = services.get('backend') if isinstance(services.get('backend'), dict) else {}
    environment = backend.get('environment') if isinstance(backend.get('environment'), dict) else {}
    tts_provider = _safe_identifier(environment, 'TTS_PROVIDER')
    tts_model = (
        _safe_model_name(environment, 'TTS_OPENAI_COMPATIBLE_MODEL')
        if tts_provider == 'openai_compatible'
        else _basename_model(environment, 'TTS_SHERPA_MODEL')
    )
    return {
        'stt_prerecorded_model': _required_text(environment, 'STT_PRERECORDED_MODEL'),
        'mlx_moss_diarize_endpoint': _safe_endpoint(
            environment,
            'MLX_MOSS_DIARIZE_ENDPOINT',
            schemes={'http', 'https'},
            required_path='/v1/audio/transcriptions',
        ),
        'mlx_moss_diarize_model': _safe_model_name(environment, 'MLX_MOSS_DIARIZE_MODEL'),
        'generic_llm_provider': _safe_identifier(environment, 'OMI_LLM_DEFAULT_PROVIDER'),
        'generic_llm_model': _safe_model_name(environment, 'OMI_LLM_DEFAULT_MODEL'),
        'generic_llm_transport': 'openai_compatible_http',
        'generic_llm_endpoint_origin': _safe_endpoint_origin(
            environment, 'GENERIC_OPENAI_BASE_URL', schemes={'http', 'https'}
        ),
        'embedding_provider': _safe_identifier(environment, 'EMBEDDING_PROVIDER'),
        'embedding_model': _safe_model_name(environment, 'EMBEDDING_MODEL'),
        'embedding_transport': _safe_identifier(environment, 'EMBEDDING_CAPABILITY_TRANSPORT'),
        'embedding_dimension': _positive_integer(environment, 'EMBEDDING_DIMENSION'),
        'realtime_provider': _safe_identifier(environment, 'REALTIME_PROVIDER'),
        'realtime_model': _safe_model_name(environment, 'REALTIME_MODEL'),
        'realtime_transport': 'websocket_relay',
        'realtime_endpoint_origin': _safe_endpoint_origin(environment, 'REALTIME_RELAY_URL', schemes={'ws', 'wss'}),
        'realtime_wire_protocol': _safe_identifier(environment, 'REALTIME_RELAY_WIRE_PROTOCOL'),
        'tts_provider': tts_provider,
        'tts_model': tts_model,
        'tts_transport': 'local' if tts_provider == 'sherpa_onnx' else 'openai_compatible_http',
        'tts_endpoint_origin': (
            _safe_endpoint_origin(environment, 'TTS_OPENAI_COMPATIBLE_BASE_URL', schemes={'http', 'https'})
            if tts_provider == 'openai_compatible'
            else ''
        ),
        'file_chat_provider': 'local_extraction',
        'file_chat_model': _safe_model_name(environment, 'OMI_LLM_DEFAULT_MODEL'),
        'file_chat_transport': _safe_identifier(environment, 'FILE_CHAT_TRANSPORT'),
        'push_provider': _safe_identifier(environment, 'PUSH_PROVIDER'),
        'push_model': 'disabled',
        'push_transport': 'disabled',
        'memory_keyword_provider': _safe_identifier(environment, 'MEMORY_KEYWORD_INDEX_PROVIDER'),
        'conversation_keyword_provider': _safe_identifier(environment, 'CONVERSATION_KEYWORD_INDEX_PROVIDER'),
        'typesense_transport': _safe_identifier(environment, 'TYPESENSE_PROTOCOL'),
        'typesense_host': _safe_host(environment, 'TYPESENSE_HOST'),
        'memory_typesense_collection': _safe_model_name(environment, 'MEMORY_TYPESENSE_COLLECTION'),
        'conversation_typesense_collection': _safe_model_name(environment, 'CONVERSATION_TYPESENSE_COLLECTION'),
    }


def collect_runtime_snapshot(
    *, compose_file: Path, env_file: Path, effective: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    compose = ['bash', str(COMPOSE_WRAPPER), str(env_file), str(compose_file)]
    effective = effective or load_effective_compose_config(compose_file=compose_file, env_file=env_file)
    effective_services = effective.get('services') if isinstance(effective.get('services'), dict) else {}
    services: dict[str, dict[str, Any]] = {}
    for service in REQUIRED_SERVICES:
        container_id = _run([*compose, 'ps', '--quiet', service])
        if not container_id or '\n' in container_id:
            raise RuntimeError(f'could not resolve exact running container for {service}')
        inspected = json.loads(_run(['docker', 'inspect', container_id]))
        if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
            raise RuntimeError(f'docker inspect returned invalid data for {service}')
        container = inspected[0]
        state = container.get('State') if isinstance(container.get('State'), dict) else {}
        health = state.get('Health') if isinstance(state.get('Health'), dict) else {}
        config = container.get('Config') if isinstance(container.get('Config'), dict) else {}
        container_labels = config.get('Labels') if isinstance(config.get('Labels'), dict) else {}
        image_id = str(container.get('Image') or '')
        image_inspected = json.loads(_run(['docker', 'image', 'inspect', image_id]))
        if not isinstance(image_inspected, list) or len(image_inspected) != 1:
            raise RuntimeError(f'docker image inspect returned invalid data for {service}')
        image_config = image_inspected[0].get('Config') if isinstance(image_inspected[0].get('Config'), dict) else {}
        image_labels = image_config.get('Labels') if isinstance(image_config.get('Labels'), dict) else {}
        actual_environment = {}
        for binding in config.get('Env') if isinstance(config.get('Env'), list) else []:
            if isinstance(binding, str) and '=' in binding:
                key, value = binding.split('=', 1)
                actual_environment[key] = value
        expected_service = effective_services.get(service) if isinstance(effective_services.get(service), dict) else {}
        expected_environment = (
            expected_service.get('environment') if isinstance(expected_service.get('environment'), dict) else {}
        )
        services[service] = {
            'state': state.get('Status'),
            'health': health.get('Status'),
            'image_id': image_id,
            'source_git_commit': image_labels.get('com.omi.source.git-commit'),
            'source_git_tree': image_labels.get('com.omi.source.git-tree'),
            'runtime_config_sha256': container_labels.get('com.omi.runtime.config-sha256'),
            'environment_matches_effective_config': all(
                actual_environment.get(str(key)) == str(value) for key, value in expected_environment.items()
            ),
        }
    return services


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--compose-file', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--expected-git-commit', required=True)
    parser.add_argument('--expected-git-tree', required=True)
    parser.add_argument('--expected-config-sha256', required=True)
    arguments = parser.parse_args()
    try:
        effective = load_effective_compose_config(compose_file=arguments.compose_file, env_file=arguments.env_file)
        actual_config_sha256 = canonical_effective_config_sha256(effective)
        if actual_config_sha256 != arguments.expected_config_sha256:
            raise ValueError('effective reviewed Compose configuration changed after the attributed stack was started')
        result = validate_runtime_snapshot(
            services=collect_runtime_snapshot(
                compose_file=arguments.compose_file,
                env_file=arguments.env_file,
                effective=effective,
            ),
            expected_git_commit=arguments.expected_git_commit,
            expected_git_tree=arguments.expected_git_tree,
            expected_config_sha256=arguments.expected_config_sha256,
            effective_provider_configuration=effective_provider_configuration(effective),
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f'ERROR: runtime evidence failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
