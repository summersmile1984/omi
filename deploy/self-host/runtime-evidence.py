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


def validate_runtime_snapshot(
    *,
    services: dict[str, dict[str, Any]],
    expected_git_commit: str,
    expected_git_tree: str,
    expected_config_sha256: str,
    effective_provider_configuration: dict[str, str],
) -> dict[str, Any]:
    if not OBJECT_ID.fullmatch(expected_git_commit) or not OBJECT_ID.fullmatch(expected_git_tree):
        raise ValueError('expected source commit/tree must be full Git object IDs')
    if not SHA256.fullmatch(expected_config_sha256):
        raise ValueError('expected runtime config hash must be a sha256 digest')
    required_provider_configuration = {
        'stt_prerecorded_model',
        'mlx_moss_diarize_endpoint',
        'mlx_moss_diarize_model',
    }
    if (
        set(effective_provider_configuration) != required_provider_configuration
        or effective_provider_configuration.get('stt_prerecorded_model') != 'mlx_moss_diarize'
        or not all(effective_provider_configuration.values())
    ):
        raise ValueError('effective provider configuration is incomplete or selected the wrong prerecorded provider')

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


def effective_provider_configuration(effective: dict[str, Any]) -> dict[str, str]:
    services = effective.get('services') if isinstance(effective.get('services'), dict) else {}
    backend = services.get('backend') if isinstance(services.get('backend'), dict) else {}
    environment = backend.get('environment') if isinstance(backend.get('environment'), dict) else {}
    return {
        'stt_prerecorded_model': str(environment.get('STT_PRERECORDED_MODEL') or ''),
        'mlx_moss_diarize_endpoint': str(environment.get('MLX_MOSS_DIARIZE_ENDPOINT') or ''),
        'mlx_moss_diarize_model': str(environment.get('MLX_MOSS_DIARIZE_MODEL') or ''),
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
