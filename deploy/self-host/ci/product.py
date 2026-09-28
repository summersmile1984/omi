#!/usr/bin/env python3
"""Isolated real Server OS target for the common HTTP product contract.

LIFECYCLE: permanent
This owns a fresh Compose project, normal migrations and application images.
The complete rendered profile runs in one real runtime shape:

  - hosted operator AI: a single OpenAI-compatible vendor (openrouter /
    siliconflow / cloudflare-gateway / mimo-cn) declared by the brand
    manifest supplies LLM / ASR / TTS over the wire; only the local BGE-M3
    embedding store is still required (none of the hosted vendors own that
    model).

A native local text model is no longer a runtime shape: `fork.bootstrap`
refuses a profile row that carries a local `llm`, so a fixture without a
declared operator credential fails before any state is created.

Missing model requirements fail before fixture state is created.
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
MODEL_APPLICATION_HEADROOM = 4 * 1024**3
PYTHON_BASE = 'python:3.11.10-slim-bookworm@sha256:840e180ebcc6e5c8efab209c43f5e40fd2af98cb49db5c7103c90539c56bb30e'
sys.path.insert(0, str(ROOT / 'scripts/profiles'))
import render  # noqa: E402
sys.path.insert(0, str(ROOT / 'deploy/self-host'))
from model_services import selected_config  # noqa: E402

# Vendors other than MiMo own every capability (LLM / ASR / TTS / embedding)
# over the wire; the canonical Compose wrapper removes the local model
# services for them via deploy/self-host/model_services.py::specialize. The
# fixture renders the same profile through specialize() so the compose it
# starts matches the production admission surface exactly.
HOSTED_OPERATORS = frozenset({'openrouter', 'siliconflow', 'cloudflare-gateway'})


def _vendor_hosts(provider):
    """External HTTPS hosts a hosted operator exposes for chat/embedding/ASR/TTS.

    The backend's HttpEndpoint guard admits requests only to hosts listed in
    SELF_HOST_EGRESS_ALLOWLIST; for a hosted operator the only such host is
    the vendor itself. Returning the union of all three keeps the fixture
    env-shareable without removing mimo-cn's local-embedding path.
    """
    return {
        'openrouter': {'openrouter.ai'},
        'siliconflow': {'api.siliconflow.cn'},
        'cloudflare-gateway': {'api.cloudflare.com'},
    }.get(provider, set())


class Fixture:
    def __init__(self, output, brand_id, port, runtime_image=None, *, model_stores=None, mimo_secret_file=None, operator_secret_file=None, operator_provider=None):
        if not re.fullmatch(r'[a-z][a-z0-9-]{2,40}', brand_id) or not 1024 <= port <= 65000:
            raise ValueError('fixture needs a safe brand id and unprivileged port')
        self.model_stores = {}
        # mimo_secret_file is the legacy single-provider form; operator_secret_file
        # generalises it to every HostedOperatorAI vendor registered in
        # backend/fork/operator_ai.py. The two are mutually exclusive.
        self.mimo_secret_file = Path(mimo_secret_file).resolve() if mimo_secret_file else None
        self.operator_secret_file = Path(operator_secret_file).resolve() if operator_secret_file else None
        self.operator_provider = operator_provider
        if self.mimo_secret_file and self.operator_secret_file:
            raise ValueError('pass either --mimo-secret-file or --operator-secret-file, not both')
        if self.operator_secret_file:
            if self.operator_provider is None:
                raise ValueError('--operator-provider is required with --operator-secret-file')
            if self.operator_provider not in {'mimo-cn', 'openrouter', 'cloudflare-gateway', 'siliconflow'}:
                raise ValueError(f'unsupported operator provider: {self.operator_provider}')
            if not self.operator_secret_file.is_file():
                raise ValueError('operator secret file must exist')
        if self.mimo_secret_file and not self.mimo_secret_file.is_file():
            raise ValueError('MiMo secret file must exist')
        if not (self.mimo_secret_file or self.operator_secret_file):
            raise ValueError(
                'the product fixture requires --mimo-secret-file or --operator-secret-file; '
                'a native local text model is refused at self-host admission'
            )
        # The MiMo-managed profile keeps its local BGE-M3 embedding store; a
        # hosted operator AI (openrouter / siliconflow / cloudflare-gateway)
        # owns embeddings too, so the fixture starts without any model store.
        hosted = bool(self.operator_secret_file) and self.operator_provider in HOSTED_OPERATORS
        if hosted:
            if model_stores not in (None, {}):
                raise ValueError('hosted operator AI owns embeddings; pass no --embedding-store')
        elif model_stores is None or set(model_stores) != {'embedding'} or not all(model_stores.values()):
            raise ValueError('real-model fixture requires the embedding store only')
        if self.mimo_secret_file:
            from fork.operator_ai import MiMo

            credential = json.loads(self.mimo_secret_file.read_text())
            if credential.get('MIMO_BASE_URL') != MiMo().base_url or not credential.get('MIMO_API_KEY'):
                raise ValueError('MiMo credential must select the China Token Plan endpoint')
        elif self.operator_secret_file:
            # Every hosted vendor authenticates with one bearer; the per-provider
            # env var name is in fork.operator_ai.CREDENTIAL_ENV. The secret file
            # carries only the key for the vendor that the brand manifest already
            # declared, so there is nothing else to validate here -- the operator
            # AI module does that when it reads the key at runtime.
            credential = json.loads(self.operator_secret_file.read_text())
            if not isinstance(credential, dict) or not credential:
                raise ValueError('operator secret file must be a non-empty JSON object')
        for kind, path in (model_stores or {}).items():
            path = Path(path)
            if not path.is_absolute() or not path.is_dir():
                raise ValueError(f'{kind} model store must be an existing absolute directory')
            self.model_stores[kind] = path.resolve()
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
        # Bind the production Compose wrapper as an attribute so unit tests
        # can monkey-patch it; the real call site still goes through the
        # deploy/self-host/model_services.py canon.
        self.selected_config = selected_config

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

    def admit_model_capacity(self, services):
        # Hosted operator AI does not bind any local model service, so the
        # engine-memory admission only applies to the MiMo shape.
        if not self.model_stores:
            total = int(self.command(['docker', 'info', '--format', '{{.MemTotal}}'], capture=True, timeout=30).strip())
            report = {
                'engine_memory_bytes': total,
                'model_memory_limits': {},
                'application_headroom_bytes': MODEL_APPLICATION_HEADROOM,
                'required_engine_memory_bytes': MODEL_APPLICATION_HEADROOM,
                'admitted': total >= MODEL_APPLICATION_HEADROOM,
            }
            (self.output / 'model-capacity.json').write_text(json.dumps(report, indent=2) + '\n')
            if not report['admitted']:
                raise RuntimeError(
                    f'real-model fixture needs at least {MODEL_APPLICATION_HEADROOM / 1024**3:g} GiB Docker memory '
                    f'for application headroom; engine reports {total / 1024**3:.2f} GiB'
                )
            return
        limits = {name: services[name].get('mem_limit') for name in ('embedding',) if name in self.model_stores}
        # `docker compose config --format json` emits byte counts as decimal
        # strings, including the production YAML's `4g` model limits.
        if any(not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]*', value) for value in limits.values()):
            raise ValueError('real-model fixture requires explicit Compose model memory limits')
        limits = {name: int(value) for name, value in limits.items()}
        total = int(self.command(['docker', 'info', '--format', '{{.MemTotal}}'], capture=True, timeout=30).strip())
        required = sum(limits.values()) + MODEL_APPLICATION_HEADROOM
        report = {
            'engine_memory_bytes': total,
            'model_memory_limits': limits,
            'application_headroom_bytes': MODEL_APPLICATION_HEADROOM,
            'required_engine_memory_bytes': required,
            'admitted': total >= required,
        }
        (self.output / 'model-capacity.json').write_text(json.dumps(report, indent=2) + '\n')
        if not report['admitted']:
            raise RuntimeError(
                f'real-model fixture needs at least {required / 1024**3:g} GiB Docker memory '
                f'for model limits and application headroom; engine reports {total / 1024**3:.2f} GiB'
            )

    def _selected_operator(self):
        """The render-time operator selection for the credential actually passed.

        Hosted operators must reach the profile row: render.resolve() installs
        the selection through operator_ai.configure(), which strips the native
        llm/speech rows that fork.bootstrap otherwise refuses at admission.
        Passing None here (as this fixture did before) renders a native row and
        every backend container dies with 'remove row.llm'.
        """
        if self.mimo_secret_file:
            return 'mimo-cn'
        if self.operator_secret_file:
            return self.operator_provider
        return None

    def _brand_manifest(self):
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
        # The canonical production wrapper (deploy/self-host/model_services
        # .profile_for -> scripts/profiles/render.resolve) reads
        # `self_hosted_inference.<stage>` from the brand manifest, NOT the
        # operator_ai positional argument. The fixture's brand manifest is
        # synthesized, so the rendered selection must be encoded here too,
        # otherwise profile_for sees operator_ai=None and specialize() takes
        # the early-return path that skips every operator-aware wiring.
        operator = self._selected_operator()
        if operator:
            manifest.setdefault('self_hosted_inference', {})['local'] = operator
        if self.operator_provider == 'cloudflare-gateway' and not manifest.get('cloudflare_ai_gateway'):
            # cloudflare_spec() derives the account-scoped REST origin from
            # these public ids and refuses a non-dict; the fixture manifest is
            # synthesized, so pin deterministic values (same shape the gateway
            # contract test uses) instead of failing on the real brand's None.
            manifest['cloudflare_ai_gateway'] = {
                'account_id': 'a' * 32,
                'gateway_id': 'product-fixture',
            }
        return manifest

    def _render_profile(self, manifest):
        # The fixture writes the rendered brand manifest at a stable in-repo
        # path so the canonical production wrapper (deploy/self-host/
        # model_services.selected_config -> profile_for) can resolve it the
        # same way it resolves any reviewed deployment configuration.
        manifest_file = ROOT / 'deploy/self-host/ci-rendered-product-manifest.json'
        manifest_file.write_text(json.dumps(manifest))
        table = render.resolve(
            'self_hosted', None, manifest_file, 'local', self._selected_operator()
        )
        profile_file = self.output / 'profile.json'
        profile_file.write_text(json.dumps(table, indent=2) + '\n')
        # The public profile is bind-mounted into a different Linux UID. The
        # enclosing fixture and credential files retain the private umask.
        profile_file.chmod(0o444)
        return table

    def prepare(self):
        manifest = self._brand_manifest()
        self._render_profile(manifest)
        api, auth = f'http://127.0.0.1:{self.port}', f'http://127.0.0.1:{self.port + 1}'
        profile_file = self.output / 'profile.json'
        env = {}
        for line in (ROOT / 'deploy/self-host/.env.production.example').read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env[key] = secrets.token_hex(24) if 'REPLACE_' in value else value
        # `selected_config()` (deploy/self-host/model_services.py) reads the
        # brand manifest by this exact path; the canonical production wrapper
        # uses the same resolution. The synthetic manifest is the rendered
        # source of truth for this fixture run.
        manifest_path = 'deploy/self-host/ci-rendered-product-manifest.json'
        env.update(
            SELF_HOST_STAGE='local',
            SELF_HOST_BRAND_MANIFEST=manifest_path,
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
            # SELF_HOST_STAGE=local shares the PostgreSQL instance as the
            # vector store (backend/fork/bootstrap.py). The pgvector image
            # pin matches dev/docker-compose.dev.yml exactly so a fixture
            # run and a local dev stack agree on the extension owner.
            POSTGRES_IMAGE='pgvector/pgvector:pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b',
            VECTOR_STORE_PROVIDER='pgvector',
            PGVECTOR_COLLECTION_PREFIX='contract',
            BETTER_AUTH_TRUSTED_ORIGINS=auth,
            SELF_HOST_EGRESS_ALLOWLIST='embedding',
            BACKEND_RUNTIME_IMAGE=self.runtime_image,
            BACKEND_IMAGE=self.api_image,
            AUTH_SERVER_IMAGE=self.auth_image,
            QDRANT_COLLECTION_PREFIX='contract',
            VECTOR_PROJECTION_MODE='single',
            VECTOR_PROJECTION_ACTIVE_VERSION='v1',
            VECTOR_PROJECTION_SCHEMA_VERSION='1',
            VECTOR_PROJECTION_DELETE_VERSIONS='v1',
        )
        # The local embedding store and the synthetic "controlled-unavailable"
        # GENERIC_OPENAI / REALTIME_RELAY bindings only exist when the
        # profile's operator AI is MiMo. Hosted vendors own embeddings over
        # the wire; the egress allowlist names the actual vendor hosts so the
        # backend's HttpEndpoint guard admits every capability the vendor
        # exposes (fork/operator_ai._grants is the per-endpoint authority).
        if self.operator_provider in HOSTED_OPERATORS:
            env['SELF_HOST_EGRESS_ALLOWLIST'] = ','.join(
                {
                    'openrouter.ai',
                    'api.siliconflow.cn',
                    'api.cloudflare.com',
                }
                & _vendor_hosts(self.operator_provider)
            )
        else:
            env['EMBEDDING_MODEL_STORE'] = str(self.model_stores['embedding'])
            env['SPEECH_MODEL_STORE'] = str(self.output / 'unused-speech-store')
            env['GENERIC_OPENAI_BASE_URL'] = 'http://embedding:11434/v1'
            env['GENERIC_OPENAI_MODEL'] = 'controlled-unavailable'
            env['GENERIC_OPENAI_API_KEY'] = secrets.token_hex(24)
            env['REALTIME_RELAY_URL'] = 'ws://embedding:11434/unavailable'
            env['REALTIME_RELAY_ALLOWED_HOSTS'] = 'embedding'
            env['REALTIME_RELAY_PROVIDER_ID'] = 'controlled-unavailable'
            env['REALTIME_MODEL'] = 'controlled-unavailable'
        env['POSTGRES_PASSWORD_URLENCODED'] = env['POSTGRES_PASSWORD']
        env_file = self.output / 'fixture.env'
        env_file.write_text(''.join(f'{key}={value}\n' for key, value in env.items()))
        # Canonical production wrapper: read the operator from the brand
        # manifest through profile_for(), then specialize() drops every
        # local model service the operator owns over the wire. The same
        # code path backs deploy/self-host/check-config.py --env-file, so
        # the fixture admission surface matches the deployed one byte for
        # byte. The upstream compose.production.yml is the only input;
        # docker is not invoked for graph construction.
        config = self.selected_config(env)
        # The upstream compose stores each service environment as a list of
        # `KEY=VALUE` entries; the rest of this fixture and every Docker
        # JSON consumer below expect an explicit dict, so normalize once
        # here. YAML list entries like `${VAR:?VAR is required}` are kept
        # verbatim for VAR substitution, which already happened inside
        # `selected_config()`.
        for service in config['services'].values():
            environment = service.get('environment')
            if isinstance(environment, list):
                merged = {}
                for entry in environment:
                    if not isinstance(entry, str):
                        raise ValueError('compose environment entries must be strings')
                    key, _, value = entry.partition('=')
                    merged[key] = value
                service['environment'] = merged
        # The 2026-09-05 real-model run exhausted an 8 GiB Docker VM during
        # finalization and killed llama-server. Reject that known insufficient
        # allocation before building or starting any application containers.
        self.admit_model_capacity(config['services'])
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
            'pgvector-migrate',
            'auth-server',
            'backend',
            'queue-worker',
            'memory-maintenance-worker',
        )
        # specialize() drops embedding/embedding-artifact-check when the
        # operator owns embeddings; for MiMo the local BGE-M3 service stays
        # and admission expects to see its services in the compose graph.
        if self.operator_provider not in HOSTED_OPERATORS:
            selected += ('embedding-artifact-check', 'embedding')
        services = {name: config['services'][name] for name in selected}
        for name, service in services.items():
            service.pop('build', None)
            service.pop('depends_on', None)
            service.pop('extra_hosts', None)
            service['restart'] = 'no'
            service['networks'] = ['default']
            if name not in ('auth-server', 'backend', 'minio'):
                service.pop('ports', None)
            if name in ('backend', 'queue-worker') and 'speech' not in self.model_stores:
                service['volumes'] = [{'type': 'volume', 'source': 'backend-syncing', 'target': '/app/syncing'}]
            if 'healthcheck' in service:
                service['healthcheck'].update(interval='2s', start_period='2s', retries=60)
        if self.mimo_secret_file:
            from fork.operator_ai import MiMo

            credential = json.loads(self.mimo_secret_file.read_text())
            if credential.get('MIMO_BASE_URL') != MiMo().base_url or not credential.get('MIMO_API_KEY'):
                raise ValueError('MiMo credential must select the China Token Plan endpoint')
            for name in ('backend', 'memory-maintenance-worker'):
                services[name]['networks'].append('client')
                services[name]['environment']['MIMO_API_KEY'] = credential['MIMO_API_KEY']
        elif self.operator_secret_file:
            # Every hosted vendor authenticates with one bearer; the per-provider
            # env var name is in fork.operator_ai.CREDENTIAL_ENV. The brand manifest
            # has already declared which provider this stage selects, so the
            # secret file only carries the matching key.
            from fork.operator_ai import CREDENTIAL_ENV, FROZEN, CLOUDFLARE_GATEWAY, cloudflare_spec

            credential = json.loads(self.operator_secret_file.read_text())
            if self.operator_provider == 'mimo-cn':
                env_var = 'MIMO_API_KEY'
            elif self.operator_provider == 'cloudflare-gateway':
                env_var = 'CLOUDFLARE_API_TOKEN'
            else:
                env_var = CREDENTIAL_ENV[self.operator_provider]
            key = credential.get(env_var)
            if not key:
                raise ValueError(f'operator secret file missing {env_var}')
            for name in ('backend', 'memory-maintenance-worker'):
                services[name]['networks'].append('client')
                services[name]['environment'][env_var] = key
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
        used_volumes = set()
        for service in services.values():
            for mount in service.get('volumes', []):
                if isinstance(mount, dict):
                    if mount.get('type') == 'volume' and mount.get('source'):
                        used_volumes.add(mount['source'])
                elif isinstance(mount, str):
                    # Long-form `source:target[:mode]` shorthand; the source
                    # is the named volume, so it must exist at the top level.
                    source = mount.split(':', 1)[0].strip()
                    if source and not source.startswith('/') and not source.startswith('.'):
                        used_volumes.add(source)
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
                    'scope': 'real-model-product-runtime',
                    'profile_sha256': hashlib.sha256(profile_file.read_bytes()).hexdigest(),
                    'speech': ('MiMo-CN-ASR-TTS' if self.mimo_secret_file else 'admitted-local-SenseVoice-Kokoro'),
                    'llm': (
                        'MiMo-CN-mimo-v2.5'
                        if self.mimo_secret_file
                        else f'hosted-operator-{self.operator_provider}'
                    ),
                    'embedding': (
                        'admitted-local-BGE-M3-Ollama'
                        if self.mimo_secret_file
                        else f'hosted-operator-{self.operator_provider}-bge-m3'
                    ),
                    'application_network': 'API-outbound-selected-MiMo' if self.mimo_secret_file else 'internal-only',
                    'http_ingress': 'isolated-two-port-loopback-proxy',
                    'websocket_ingress': 'same-proxy-bounded-bidirectional-tunnel',
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
        shutil.copyfile(ROOT / 'backend/requirements-fork.txt', self.output / 'requirements.txt')
        (self.output / '.dockerignore').write_text('*\n!Dockerfile\n!requirements.txt\n!profile.json\n')
        (self.output / 'Dockerfile').write_text(
            'ARG BASE\nFROM ${BASE}\nUSER root\nCOPY requirements.txt /tmp/fork-requirements.txt\n'
            'RUN python -m pip install --no-cache-dir --no-deps --require-hashes -r /tmp/fork-requirements.txt\n'
            'ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache\n'
            'RUN python /app/scripts/prewarm_tiktoken_cache.py '
            '&& chmod 0555 /opt/tiktoken-cache && chmod 0444 /opt/tiktoken-cache/*\n'
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
        # The real image must tokenize on a cold process without any network or
        # writable cache. A host prewarm or an already-running process cannot
        # satisfy this regression check (recorded retrieval failure, 2026-09-05).
        self.command(
            [
                'docker',
                'run',
                '--rm',
                '--read-only',
                '--network=none',
                '--platform=linux/amd64',
                '--user=10001:10001',
                self.api_image,
                'python',
                '-c',
                'import tiktoken; e=tiktoken.encoding_for_model("gpt-4"); '
                's="Eddy understands 茉莉花茶"; ids=e.encode(s); '
                'assert ids and e.decode(ids)==s; print("offline tokenizer round-trip passed")',
            ],
            timeout=60,
        )

    def start(self):
        self.created = True
        # MiMo keeps its local BGE-M3 service; hosted operator AI does not
        # ship embedding or its artifact check, so specialize() removed them
        # from the compose graph and there is nothing to run here.
        if self.operator_provider not in HOSTED_OPERATORS:
            self.compose('run', '--rm', 'embedding-artifact-check')
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
        else:
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
            )
        for service in ('auth-migrate', 'firestore-pg-migrate', 'pgvector-migrate', 'qdrant-migrate'):
            self.compose('run', '--rm', service)
        self.compose(
            'up',
            '-d',
            '--wait',
            '--wait-timeout',
            '600',
            'auth-server',
            'backend',
            'queue-worker',
            'memory-maintenance-worker',
            timeout=630,
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
    parser.add_argument(
        '--mimo-secret-file', type=Path, help='local MiMo CN LLM/ASR/TTS; requires only --embedding-store'
    )
    parser.add_argument(
        '--operator-secret-file',
        type=Path,
        help='hosted operator AI secret JSON; one bearer per the chosen provider, requires --operator-provider; --embedding-store is unused',
    )
    parser.add_argument(
        '--operator-provider',
        choices=['mimo-cn', 'openrouter', 'cloudflare-gateway', 'siliconflow'],
        help='provider declared by the brand manifest under self_hosted_inference.<stage>; the secret file must carry its matching env var',
    )
    parser.add_argument(
        '--embedding-store',
        type=Path,
        help='admitted BGE-M3 store; required only for MiMo. Hosted operator AI owns embeddings over the wire.',
    )
    parser.add_argument('--self-test', action='store_true', help='run common HTTP contract and clean up')
    args = parser.parse_args()
    hosted = args.operator_provider in {'openrouter', 'cloudflare-gateway', 'siliconflow'}
    if args.embedding_store is None and not hosted:
        parser.error('--embedding-store is required when the operator is not a hosted AI')
    if args.embedding_store is not None and hosted:
        parser.error('--embedding-store must not be passed for a hosted operator AI; it owns embeddings')
    stores = {'embedding': args.embedding_store} if args.embedding_store else None
    fixture = Fixture(
        args.output,
        args.brand_id,
        args.port,
        args.runtime_image,
        model_stores=stores,
        mimo_secret_file=args.mimo_secret_file,
        operator_secret_file=args.operator_secret_file,
        operator_provider=args.operator_provider,
    )
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
