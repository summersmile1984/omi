#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Validate startup source closure and reviewed self-host configuration.

This is a startup gate, not a provider/egress/cutover attestation. It catches the
missing migration command and image/entrypoint mismatch shipped in fork PR #7.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / 'deploy/self-host/compose.production.yml'
REQUIRED_SOURCE = (
    'backend/fork/bootstrap.py',
    'backend/fork/main.py',
    'backend/fork/brand_transport.py',
    'backend/fork/profile.py',
    'backend/fork/embedding.py',
    'backend/fork/model_contract.py',
    'backend/fork/model_store.py',
    'backend/fork/speech.py',
    'backend/fork/speech_assets.py',
    'backend/fork/speech_transport.py',
    'backend/fork/memory_maintenance_worker.py',
    'deploy/self-host/prepare-speech.py',
    'backend/fork/vector_qdrant.py',
    'backend/fork/worker.py',
    'backend/fork/migrate.py',
    'backend/fork/queue_config.py',
    'backend/firestore_pg/migrations.py',
    'backend/Dockerfile',
    'backend/requirements-fork.txt',
    'deploy/self-host/auth-runtime.mjs',
    'deploy/self-host/Dockerfile',
    'deploy/self-host/build-images.sh',
    'deploy/self-host/operations.sh',
    'deploy/self-host/compose-clean-env.sh',
    'deploy/self-host/volume-snapshot.py',
    'deploy/self-host/runtime-evidence.py',
)


def check_sources(root: Path = ROOT) -> None:
    for name in REQUIRED_SOURCE:
        path = root / name
        if not path.is_file():
            raise ValueError(f'startup source missing: {name}')
        if path.suffix == '.py':
            ast.parse(path.read_text(), filename=name)
    compose = yaml.safe_load((root / 'deploy/self-host/compose.production.yml').read_text())
    for name, service in compose['services'].items():
        build = service.get('build', {})
        if build and not (root / build['dockerfile']).is_file():
            raise ValueError(f'{name}: Dockerfile is missing')
        command = service.get('command', [])
        if command and command[0] == 'python':
            if len(command) < 3 or command[1] != '-m':
                raise ValueError(f'{name}: Python service must use an explicit module entrypoint')
            module = command[2]
            if not (root / 'backend' / (module.replace('.', '/') + '.py')).is_file():
                raise ValueError(f'{name}: Python entrypoint missing: {module}')
    for name, role in (('auth-server', 'serve'), ('auth-migrate', 'migrate')):
        service = compose['services'][name]
        if service.get('command') != ['node', 'self-host-runtime.mjs', role]:
            raise ValueError(f'{name}: stage-aware Auth entrypoint is required')
        if 'SELF_HOST_STAGE=${SELF_HOST_STAGE:-production}' not in service.get('environment', []):
            raise ValueError(f'{name}: Auth stage must come from SELF_HOST_STAGE')
    if (
        compose['services']['backend'].get('depends_on', {}).get('qdrant-migrate', {}).get('condition')
        != 'service_completed_successfully'
    ):
        raise ValueError('backend: successful Qdrant migration is required before serving')
    for name in ('backend', 'queue-worker', 'memory-maintenance-worker', 'firestore-pg-migrate', 'qdrant-migrate'):
        if compose['services'][name]['build']['dockerfile'] != 'deploy/self-host/Dockerfile':
            raise ValueError(f'{name}: the self-host profile image layer is required')
    maintenance = compose['services']['memory-maintenance-worker']
    required_dependencies = {
        'embedding': 'service_healthy',
        'firestore-pg-migrate': 'service_completed_successfully',
        'qdrant-migrate': 'service_completed_successfully',
        'typesense': 'service_healthy',
    }
    for name, condition in required_dependencies.items():
        if maintenance.get('depends_on', {}).get(name, {}).get('condition') != condition:
            raise ValueError(f'memory-maintenance-worker: {name} must satisfy {condition}')


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError(f'env line {number}: expected KEY=value')
        if key in values:
            raise ValueError(f'env line {number}: duplicate key {key}')
        values[key] = value.strip('"\'')
    return values


def check_environment(path: Path) -> None:
    if path.name.endswith('.example'):
        raise ValueError('an example file is not reviewed deployment configuration')
    values = read_env(path)
    required = set(re.findall(r'\$\{([A-Z0-9_]+):\?', COMPOSE.read_text()))
    missing = sorted(name for name in required if not values.get(name))
    if missing:
        raise ValueError('required configuration missing: ' + ', '.join(missing))
    placeholders = sorted(name for name, value in values.items() if 'REPLACE_' in value)
    if placeholders:
        raise ValueError('unconfigured placeholders: ' + ', '.join(placeholders))
    stage = values.get('SELF_HOST_STAGE', 'production')
    if stage not in ('local', 'beta', 'production'):
        raise ValueError('SELF_HOST_STAGE must be local, beta, or production')
    manifest = (ROOT / values['SELF_HOST_BRAND_MANIFEST']).resolve()
    if not manifest.is_relative_to(ROOT) or not manifest.is_file():
        raise ValueError('SELF_HOST_BRAND_MANIFEST must be an existing file inside the build context')
    command = [
        sys.executable,
        str(ROOT / 'scripts/profiles/render.py'),
        '--target',
        'self_hosted',
        '--manifest',
        str(manifest),
        '--stage',
        stage,
        '--emit-json',
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode:
        raise ValueError('brand/profile cannot be rendered; validate the manifest with scripts/profiles/render.py')
    row = json.loads(result.stdout)['profiles'][f'self_hosted.{stage}']
    for env_name, profile_name in {
        'PUBLIC_BACKEND_URL': 'api_base_url',
        'PUBLIC_AUTH_URL': 'auth_base_url',
        'PUBLIC_MCP_URL': 'mcp_base_url',
        'PUBLIC_OBJECTS_URL': 'objects_base_url',
        'OMI_SHARE_BASE_URL': 'share_base_url',
    }.items():
        if values.get(env_name, '').rstrip('/') != row[profile_name].rstrip('/'):
            raise ValueError(f'{env_name} differs from the image profile; change the public manifest and env together')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--env-file', type=Path)
    args = parser.parse_args()
    try:
        check_sources()
        if not args.self_check:
            if args.env_file is None:
                parser.error('--env-file is required for deployment validation')
            check_environment(args.env_file)
    except (ValueError, KeyError, OSError, SyntaxError) as error:
        print(f'self-host startup check failed: {error}', file=sys.stderr)
        return 1
    print('self-host startup source/config check OK (provider and cutover acceptance are separate)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
