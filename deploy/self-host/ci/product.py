#!/usr/bin/env python3
"""Isolated real Server OS target for the common HTTP product contract.

LIFECYCLE: permanent
This owns a fresh Compose project, normal migrations and application images.
Its explicit core-only profile disables speech; only the embedding HTTP
provider is controlled. There are no route, identity or persistence substitutes.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[3]
PYTHON_BASE = 'python:3.11.10-slim-bookworm@sha256:840e180ebcc6e5c8efab209c43f5e40fd2af98cb49db5c7103c90539c56bb30e'
sys.path.insert(0, str(ROOT / 'scripts/profiles'))
import render  # noqa: E402


class Fixture:
    def __init__(self, output, brand_id, port, runtime_image=None):
        if not re.fullmatch(r'[a-z][a-z0-9-]{2,40}', brand_id) or not 1024 <= port <= 65000:
            raise ValueError('fixture needs a safe brand id and unprivileged port')
        os.umask(0o077)
        output.mkdir(parents=True, exist_ok=False)
        self.output = output.resolve()
        self.project = 'memweft-contract-' + secrets.token_hex(6)
        self.brand_id, self.port = brand_id, port
        self.runtime_image = runtime_image or self.project + '-base'
        self.auth_image = self.project + '-auth'
        self.api_image = self.project + '-api'
        self.compose_file = self.output / 'compose.json'
        self.stopped = threading.Event()
        self.created = False
        self.sequence = 0
        self.active = None
        self.closing = False

    def stop(self, *_args):
        self.stopped.set()
        if self.active is not None and not self.closing:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.active.pid, signal.SIGKILL)

    def command(self, args, *, capture=False, timeout=300, input=None):
        if self.stopped.is_set() and not self.closing:
            raise RuntimeError('fixture startup was cancelled')
        self.sequence += 1
        log = self.output / f'command-{self.sequence:02d}.log'
        # Logs are private and subprocess arguments are never echoed: the
        # rendered Compose file contains synthetic per-run service credentials.
        with log.open('w') as stream:
            process = subprocess.Popen(
                args,
                cwd=ROOT,
                text=True,
                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else stream,
                stderr=stream,
                start_new_session=True,
            )
            self.active = process
            try:
                output, _ = process.communicate(input, timeout=timeout)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)
                raise
            finally:
                # A completed CLI must not leave its inherited child group alive.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                self.active = None
            if capture and output:
                stream.write(output)
        if process.returncode:
            raise RuntimeError(f'fixture command failed ({process.returncode}); inspect {log.name}')
        return output if capture else None

    def compose(self, *args, **kwargs):
        return self.command(
            [
                'bash',
                str(ROOT / 'deploy/self-host/compose-clean-env.sh'),
                str(self.output / 'fixture.env'),
                str(self.compose_file),
                '-p',
                self.project,
                *args,
            ],
            **kwargs,
        )

    def prepare(self):
        manifest = copy.deepcopy(render.load_yaml(ROOT / 'brand/omi-upstream/manifest.yaml'))
        manifest['brand'].update(id=self.brand_id, display_name='Product Fixture', short_name='Product Fixture')
        manifest['domains'] = {key: f'https://{key.replace("_", "-")}.example.invalid' for key in manifest['domains']}
        api, auth = f'http://127.0.0.1:{self.port}', f'http://127.0.0.1:{self.port + 1}'
        manifest['deployments'] = {
            'self_hosted': {
                'local': {
                    'api_base': api,
                    'auth_base': auth,
                    'web_app': api,
                    'mcp_base': api,
                    'share_base': api,
                    'objects_base': f'http://127.0.0.1:{self.port + 2}',
                }
            }
        }
        manifest_file = self.output / 'brand.json'
        manifest_file.write_text(json.dumps(manifest))
        table = render.resolve('self_hosted', None, manifest_file, 'local')
        row = table['profiles']['self_hosted.local']
        row.pop('speech', None)
        row['capabilities'].update(stt_providers=[], tts_provider='disabled')
        from fork.capabilities import validate

        validate(row)
        profile_file = self.output / 'profile.json'
        profile_file.write_text(json.dumps(table, indent=2) + '\n')
        env = {}
        for line in (ROOT / 'deploy/self-host/.env.production.example').read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env[key] = secrets.token_hex(24) if 'REPLACE_' in value else value
        env.update(
            SELF_HOST_STAGE='local',
            SELF_HOST_BRAND_MANIFEST='brand/omi-upstream/manifest.yaml',
            SELF_HOST_BIND_ADDRESS='127.0.0.1',
            BACKEND_PORT=str(self.port),
            AUTH_SERVER_PORT=str(self.port + 1),
            MINIO_API_PORT=str(self.port + 2),
            MINIO_CONSOLE_PORT=str(self.port + 3),
            PUBLIC_BACKEND_URL=api,
            PUBLIC_AUTH_URL=auth,
            PUBLIC_MCP_URL=api,
            PUBLIC_OBJECTS_URL=f'http://127.0.0.1:{self.port + 2}',
            OMI_SHARE_BASE_URL=api,
            CORS_ALLOWED_ORIGINS=auth,
            BETTER_AUTH_TRUSTED_ORIGINS=auth,
            SELF_HOST_EGRESS_ALLOWLIST='embedding',
            BACKEND_RUNTIME_IMAGE=self.runtime_image,
            BACKEND_IMAGE=self.api_image,
            AUTH_SERVER_IMAGE=self.auth_image,
            EMBEDDING_MODEL_STORE=str(self.output / 'unused-model-store'),
            SPEECH_MODEL_STORE=str(self.output / 'unused-speech-store'),
            GENERIC_OPENAI_BASE_URL='http://embedding:11434/v1',
            GENERIC_OPENAI_MODEL='controlled-unavailable',
            GENERIC_OPENAI_API_KEY=secrets.token_hex(24),
            REALTIME_RELAY_URL='ws://embedding:11434/unavailable',
            REALTIME_RELAY_ALLOWED_HOSTS='embedding',
            REALTIME_RELAY_PROVIDER_ID='controlled-unavailable',
            REALTIME_MODEL='controlled-unavailable',
            QDRANT_COLLECTION_PREFIX='contract',
            VECTOR_PROJECTION_MODE='single',
            VECTOR_PROJECTION_ACTIVE_VERSION='v1',
            VECTOR_PROJECTION_SCHEMA_VERSION='1',
            VECTOR_PROJECTION_DELETE_VERSIONS='v1',
        )
        env['POSTGRES_PASSWORD_URLENCODED'] = env['POSTGRES_PASSWORD']
        env_file = self.output / 'fixture.env'
        env_file.write_text(''.join(f'{key}={value}\n' for key, value in env.items()))
        config = json.loads(
            self.command(
                [
                    'bash',
                    str(ROOT / 'deploy/self-host/compose-clean-env.sh'),
                    str(env_file),
                    str(ROOT / 'deploy/self-host/compose.production.yml'),
                    'config',
                    '--format',
                    'json',
                ],
                capture=True,
            )
        )
        # Keep actual service environments, commands, immutable state images and
        # migrations. Only network/ports/restart/storage are fixture-owned.
        selected = (
            'postgres',
            'redis',
            'minio',
            'qdrant',
            'typesense',
            'auth-migrate',
            'firestore-pg-migrate',
            'qdrant-migrate',
            'auth-server',
            'backend',
            'queue-worker',
            'memory-maintenance-worker',
        )
        services = {name: config['services'][name] for name in selected}
        for name, service in services.items():
            service.pop('build', None)
            service.pop('depends_on', None)
            service.pop('extra_hosts', None)
            service['restart'] = 'no'
            service['networks'] = ['default']
            if name not in ('auth-server', 'backend', 'minio'):
                service.pop('ports', None)
            if name in ('backend', 'queue-worker'):
                service['volumes'] = [{'type': 'volume', 'source': 'backend-syncing', 'target': '/app/syncing'}]
            if 'healthcheck' in service:
                service['healthcheck'].update(interval='2s', start_period='2s', retries=60)
        services['embedding'] = {
            'image': self.api_image,
            'platform': 'linux/amd64',
            'command': ['python', '/contract/providers.py'],
            'volumes': [
                f'{profile_file}:/contract/profile.json:ro',
                f'{ROOT / "deploy/self-host/ci/providers.py"}:/contract/providers.py:ro',
            ],
            'networks': ['default'],
            'healthcheck': {
                'test': ['CMD', 'curl', '-fsS', 'http://127.0.0.1:11434/api/tags'],
                'interval': '2s',
                'timeout': '2s',
                'retries': 30,
            },
        }
        services['loopback'] = {
            'image': self.api_image,
            'platform': 'linux/amd64',
            'command': ['python', '/contract/loopback.py'],
            'volumes': [f'{ROOT / "deploy/self-host/ci/loopback.py"}:/contract/loopback.py:ro'],
            'networks': ['default', 'client'],
            'ports': services['backend'].pop('ports') + services['auth-server'].pop('ports'),
            'healthcheck': {
                'test': ['CMD', 'curl', '-fsS', 'http://127.0.0.1:3000/ready'],
                'interval': '2s',
                'timeout': '3s',
                'retries': 30,
            },
        }
        used_volumes = {
            mount['source']
            for service in services.values()
            for mount in service.get('volumes', [])
            if isinstance(mount, dict) and mount.get('type') == 'volume'
        }
        self.compose_file.write_text(
            json.dumps(
                {
                    'services': services,
                    'volumes': {name: {} for name in used_volumes},
                    'networks': {'default': {'internal': True}, 'client': {}},
                },
                indent=2,
            )
        )
        self.metadata = {
            'api_origin': api,
            'auth_origin': auth,
            'target': 'self_hosted',
            'brand_id': self.brand_id,
            'trace_dir': str(self.output),
        }
        (self.output / 'fixture-scope.json').write_text(
            json.dumps(
                {
                    'scope': 'identity-onboarding-tasks',
                    'profile_sha256': hashlib.sha256(profile_file.read_bytes()).hexdigest(),
                    'speech': 'explicitly-disabled',
                    'embedding': 'controlled-HTTP-no-model-inference',
                    'application_network': 'internal-only',
                    'http_ingress': 'isolated-two-port-loopback-proxy',
                    'release_qualified': False,
                },
                indent=2,
            )
        )

    def build(self, reuse_runtime):
        if not reuse_runtime:
            self.command(
                [
                    'docker',
                    'build',
                    '--platform=linux/amd64',
                    '-f',
                    'backend/Dockerfile',
                    '--build-arg',
                    f'PYTHON_BASE_IMAGE={PYTHON_BASE}',
                    '-t',
                    self.runtime_image,
                    '.',
                ],
                timeout=1200,
            )
        # A provided cache is admitted against actual image bytes, not its name
        # or an operator-supplied success label. Cover every tracked Python source.
        paths = self.command(['git', 'ls-files', 'backend'], capture=True).splitlines()
        expected = {
            path.removeprefix('backend/'): hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in paths
            if path.endswith('.py')
        }
        script = 'import hashlib,json,sys;from pathlib import Path; names=json.load(sys.stdin); print(json.dumps({p:hashlib.sha256(Path("/app",p).read_bytes()).hexdigest() if Path("/app",p).is_file() else None for p in names}))'
        actual = json.loads(
            self.command(
                [
                    'docker',
                    'run',
                    '--rm',
                    '-i',
                    '--network=none',
                    '--platform=linux/amd64',
                    self.runtime_image,
                    'python',
                    '-c',
                    script,
                ],
                input=json.dumps(list(expected)),
                capture=True,
            )
        )
        if actual != expected:
            raise RuntimeError('runtime image source differs from this checkout; rebuild the standard Dockerfile')
        (self.output / 'runtime-source-hashes.json').write_text(json.dumps(expected, indent=2))
        self.command(['docker', 'build', '-f', 'auth-server/Dockerfile', '-t', self.auth_image, '.'], timeout=600)
        shutil.copyfile(ROOT / 'deploy/self-host/requirements.txt', self.output / 'requirements.txt')
        (self.output / '.dockerignore').write_text('*\n!Dockerfile\n!requirements.txt\n!profile.json\n')
        (self.output / 'Dockerfile').write_text(
            'ARG BASE\nFROM ${BASE}\nUSER root\nCOPY requirements.txt /tmp/fork-requirements.txt\n'
            'RUN python -m pip install --no-cache-dir --no-deps --require-hashes -r /tmp/fork-requirements.txt\n'
            'COPY --chown=omi:omi --chmod=0444 profile.json /app/fork/deployment_profiles.generated.json\nUSER omi\n'
            'CMD ["uvicorn","fork.main:app","--host","0.0.0.0","--port","8080","--loop","uvloop"]\n'
        )
        self.command(
            [
                'docker',
                'build',
                '--platform=linux/amd64',
                '--build-arg',
                'BASE=' + self.runtime_image,
                '-t',
                self.api_image,
                str(self.output),
            ],
            timeout=600,
        )

    def start(self):
        self.created = True
        self.compose(
            'up',
            '-d',
            '--wait',
            '--wait-timeout',
            '120',
            'postgres',
            'redis',
            'minio',
            'qdrant',
            'typesense',
            'embedding',
        )
        for service in ('auth-migrate', 'firestore-pg-migrate', 'qdrant-migrate'):
            self.compose('run', '--rm', service)
        self.compose(
            'up',
            '-d',
            '--wait',
            '--wait-timeout',
            '180',
            'auth-server',
            'backend',
            'queue-worker',
            'memory-maintenance-worker',
        )
        self.compose('up', '-d', '--wait', '--wait-timeout', '90', 'loopback')
        (self.output / 'metadata.json').write_text(json.dumps(self.metadata, indent=2))
        print(json.dumps(self.metadata), flush=True)

    def close(self):
        self.closing = True
        if self.created:
            try:
                self.compose('logs', '--no-color', timeout=30)
            finally:
                # The unguessable project owns every resource removed here.
                self.compose('down', '--volumes', '--remove-orphans', '--timeout', '10', timeout=60)

    def run_core(self):
        report = self.command(
            [
                sys.executable,
                str(ROOT / 'contracts/deployment/core.py'),
                '--metadata',
                str(self.output / 'metadata.json'),
            ],
            capture=True,
            timeout=300,
        )
        print(report, end='', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--brand-id', default='product-fixture')
    parser.add_argument('--port', type=int, default=34800)
    parser.add_argument('--runtime-image', help='reuse only after actual source-byte admission')
    parser.add_argument('--self-test', action='store_true', help='run common HTTP contract and clean up')
    args = parser.parse_args()
    fixture = Fixture(args.output, args.brand_id, args.port, args.runtime_image)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, fixture.stop)
    try:
        fixture.prepare()
        fixture.build(bool(args.runtime_image))
        fixture.start()
        if args.self_test:
            fixture.run_core()
            return 0
        fixture.stopped.wait()
        return 0
    finally:
        fixture.close()


if __name__ == '__main__':
    sys.exit(main())
