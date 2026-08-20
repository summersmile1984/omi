#!/usr/bin/env python3
"""Static production/zero-vendor contract for the self-host Compose profile."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMPOSE = ROOT / 'deploy' / 'self-host' / 'compose.production.yml'
DEFAULT_EXAMPLE_ENV = ROOT / 'deploy' / 'self-host' / '.env.production.example'
DEFAULT_BACKEND_DOCKERFILE = ROOT / 'backend' / 'Dockerfile'

REQUIRED_SERVICES = {
    'postgres',
    'redis',
    'minio',
    'qdrant',
    'auth-migrate',
    'auth-server',
    'backend',
    'queue-worker',
}
ONE_SHOT_SERVICES = {'auth-migrate'}
PINNED_REMOTE_SERVICES = {'postgres', 'redis', 'minio', 'qdrant'}
STATEFUL_MOUNTS = {
    'postgres': '/var/lib/postgresql/data',
    'redis': '/data',
    'minio': '/data',
    'qdrant': '/qdrant/storage',
    'backend': '/app/syncing',
}
REQUIRED_FIXED_BACKEND_ENV = {
    'OMI_ENV_STAGE': 'prod',
    'AUTH_PROVIDER': 'better_auth',
    'AUTH_INTERNAL_ALLOW_HTTP': 'true',
    'QUEUE_BACKEND': 'redis',
    'STORAGE_BACKEND': 'minio',
    'VECTOR_STORE_PROVIDER': 'qdrant',
    'MEMORY_KEYWORD_INDEX_PROVIDER': 'disabled',
    'OMI_LLM_DEFAULT_PROVIDER': 'generic',
    'OMI_LLM_DEFAULT_FALLBACKS': '',
    'EMBEDDING_PROVIDER': 'generic',
    'STT_SERVICE_MODELS': 'sensevoice',
    'STT_ROUTE_FALLBACK_TO_DEFAULT': 'false',
    'STT_PRERECORDED_MODEL': 'sensevoice',
    'REALTIME_PROVIDER': 'disabled',
    'WEB_SEARCH_TRANSPORT': 'disabled',
    'ADMIN_KEY_AUTH_ENABLED': 'false',
}
REQUIRED_INTERPOLATED_ENV = {
    'backend': {
        'BASE_API_URL',
        'API_BASE_URL',
        'CORS_ALLOWED_ORIGINS',
        'ENCRYPTION_SECRET',
        'AUTH_JWT_ISSUER',
        'AUTH_JWT_AUDIENCE',
        'AUTH_INTERNAL_ADMIN_SECRET',
        'FIRESTORE_PG_DSN',
        'REDIS_DB_PASSWORD',
        'QUEUE_REDIS_SYNC_WORKER_SECRET',
        'QUEUE_REDIS_AUDIO_MERGE_WORKER_SECRET',
        'QUEUE_REDIS_ACCOUNT_DELETION_WORKER_SECRET',
        'QUEUE_REDIS_FINALIZATION_WORKER_SECRET',
        'MINIO_PUBLIC_ENDPOINT',
        'MINIO_ACCESS_KEY',
        'MINIO_SECRET_KEY',
        'GENERIC_OPENAI_BASE_URL',
        'GENERIC_OPENAI_API_KEY',
        'GENERIC_OPENAI_MODEL',
        'EMBEDDING_MODEL',
        'EMBEDDING_DIMENSION',
        'VECTOR_PROJECTION_MODE',
        'VECTOR_PROJECTION_ACTIVE_VERSION',
        'VECTOR_PROJECTION_SCHEMA_VERSION',
        'VECTOR_PROJECTION_DELETE_VERSIONS',
        'QDRANT_API_KEY',
        'MCP_AUTHORIZATION_SERVER_URL',
        'MCP_RESOURCE_URL',
    },
    'auth-server': {
        'DATABASE_URL',
        'BETTER_AUTH_SECRET',
        'BETTER_AUTH_URL',
        'BETTER_AUTH_TRUSTED_ORIGINS',
        'BETTER_AUTH_IP_HEADERS',
        'AUTH_INTERNAL_ADMIN_SECRET',
        'AUTH_JWT_ISSUER',
        'AUTH_JWT_AUDIENCE',
        'AUTH_JWKS_ROTATION_SECONDS',
        'AUTH_JWKS_GRACE_SECONDS',
    },
    'auth-migrate': {
        'DATABASE_URL',
        'BETTER_AUTH_SECRET',
        'BETTER_AUTH_URL',
        'BETTER_AUTH_TRUSTED_ORIGINS',
        'BETTER_AUTH_IP_HEADERS',
        'AUTH_INTERNAL_ADMIN_SECRET',
        'AUTH_JWT_ISSUER',
        'AUTH_JWT_AUDIENCE',
        'AUTH_JWKS_ROTATION_SECONDS',
        'AUTH_JWKS_GRACE_SECONDS',
    },
}
REQUIRED_ENV_FILE_KEYS = {
    'SELF_HOST_BIND_ADDRESS',
    'BACKEND_PORT',
    'AUTH_SERVER_PORT',
    'MINIO_API_PORT',
    'MINIO_CONSOLE_PORT',
    'PUBLIC_BACKEND_URL',
    'PUBLIC_AUTH_URL',
    'PUBLIC_OBJECTS_URL',
    'CORS_ALLOWED_ORIGINS',
    'BETTER_AUTH_TRUSTED_ORIGINS',
    'BETTER_AUTH_IP_HEADERS',
    'BACKEND_IMAGE',
    'BACKEND_PLATFORM',
    'AUTH_SERVER_IMAGE',
    'POSTGRES_USER',
    'POSTGRES_DB',
    'POSTGRES_PASSWORD',
    'POSTGRES_PASSWORD_URLENCODED',
    'REDIS_PASSWORD',
    'MINIO_ACCESS_KEY',
    'MINIO_SECRET_KEY',
    'MINIO_REGION',
    'QDRANT_API_KEY',
    'QDRANT_COLLECTION_PREFIX',
    'BETTER_AUTH_SECRET',
    'AUTH_INTERNAL_ADMIN_SECRET',
    'AUTH_JWKS_ROTATION_SECONDS',
    'AUTH_JWKS_GRACE_SECONDS',
    'ENCRYPTION_SECRET',
    'QUEUE_REDIS_SYNC_WORKER_SECRET',
    'QUEUE_REDIS_AUDIO_MERGE_WORKER_SECRET',
    'QUEUE_REDIS_ACCOUNT_DELETION_WORKER_SECRET',
    'QUEUE_REDIS_FINALIZATION_WORKER_SECRET',
    'GENERIC_OPENAI_BASE_URL',
    'GENERIC_OPENAI_API_KEY',
    'GENERIC_OPENAI_MODEL',
    'EMBEDDING_MODEL',
    'EMBEDDING_DIMENSION',
    'VECTOR_PROJECTION_MODE',
    'VECTOR_PROJECTION_ACTIVE_VERSION',
    'VECTOR_PROJECTION_SCHEMA_VERSION',
    'VECTOR_PROJECTION_DELETE_VERSIONS',
    'VECTOR_PROJECTION_REQUIRED_NAMESPACES',
    'SENSEVOICE_MODEL_HOST_PATH',
}
REQUIRED_PROJECTION_NAMESPACES = {
    'ns1',
    'ns2',
    'workstream-association-v1',
    'ns_x',
    'ns3',
    'ns4',
    'ns_tchunks',
}
FORBIDDEN_ENV_PREFIXES = ('FIREBASE_', 'GOOGLE_', 'GCP_', 'PINECONE_', 'OPENAI_')
FORBIDDEN_ENV_NAMES = {'SERVICE_ACCOUNT_JSON', 'USE_VERTEX_AI', 'GEMINI_API_KEY'}
FORBIDDEN_ENDPOINT_HOSTS = (
    'api.omi.me',
    'api.openai.com',
    'googleapis.com',
    'firebaseio.com',
    'pinecone.io',
)
MACOS_MODEL_BOUNDARY_REQUIREMENTS = {
    'desktop/macos/Desktop/Sources/ProactiveAssistants/Core/GeminiClient.swift': (
        'providerNeutralBackendCapability',
        'ProactiveLaneClient.shared.complete',
        'capabilityUnavailable("proactive_tool_calling")',
    ),
    'desktop/macos/Desktop/Sources/ProactiveAssistants/Services/EmbeddingService.swift': (
        'deploymentProfile != .selfHosted',
        'EmbeddingError.capabilityUnavailable',
    ),
    'desktop/macos/Desktop/Sources/RealtimeOmni/AutoModelSelector.swift': (
        'deploymentProfile == .omiCloud',
        'no implicit provider selected',
    ),
    'desktop/macos/Desktop/Sources/FloatingControlBar/RealtimeHubSession.swift': (
        'allowsClientDirectVendorEgress',
        'Realtime model capability is unavailable for this deployment',
    ),
    'desktop/macos/Desktop/Sources/BYOKValidator.swift': (
        'allowsBYOK(deploymentProfile: deploymentProfile)',
        'routes model credentials through its backend',
    ),
    'desktop/macos/Desktop/Sources/MainWindow/Pages/ChatLabView.swift': (
        'allowsClientDirectVendorEgress',
        'routes model calls through its backend',
    ),
    'desktop/macos/Desktop/Sources/Chat/AgentRuntimeEgressPolicy.swift': (
        'allowsAgentAdapter(',
        'removeInheritedModelVendorEnvironment(',
    ),
    'desktop/macos/Desktop/Sources/ProactiveAssistants/Assistants/TaskAgent/TaskAgentManager.swift': (
        'allowsClaudeTaskAgent(',
        'AgentError.modelCapabilityUnavailable',
    ),
}
MACOS_DIRECT_MODEL_HOST_ALLOWLIST = {
    'desktop/macos/Desktop/Sources/BYOKValidator.swift',
    'desktop/macos/Desktop/Sources/FloatingControlBar/RealtimeHubSession.swift',
    'desktop/macos/Desktop/Sources/MainWindow/Pages/ChatLabView.swift',
}
DIRECT_MODEL_VENDOR_HOSTS = (
    'api.anthropic.com',
    'api.deepgram.com',
    'api.openai.com',
    'generativelanguage.googleapis.com',
)


def _service_blocks(text: str) -> dict[str, str]:
    match = re.search(r'(?ms)^services:\s*\n(?P<body>.*?)(?=^volumes:\s*$)', text)
    if not match:
        return {}
    body = match.group('body')
    services: dict[str, str] = {}
    headers = list(re.finditer(r'(?m)^  ([a-z0-9][a-z0-9-]*):\s*$', body))
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(body)
        services[header.group(1)] = body[header.end() : end]
    return services


def _environment(block: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(r'(?m)^      - ([A-Z][A-Z0-9_]*)=(.*)$', block):
        result[match.group(1)] = match.group(2).strip()
    return result


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ValueError(f'{path}:{line_number}: expected KEY=value')
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip()
    return values


def _required_interpolation(value: str) -> bool:
    return bool(re.search(r'\$\{[A-Z][A-Z0-9_]*:\?[^}]+\}', value))


def validate_macos_client_model_egress(root: Path = ROOT) -> list[str]:
    """Keep self-hosted model traffic behind the tested deployment boundary.

    This is a narrow forbidden-pattern tripwire. Behavioral Swift tests prove
    the profile decisions and pre-transport refusal; this scan prevents a new
    vendor URL or an accidental deletion of those call-site guards.
    """
    errors: list[str] = []
    for relative, required_tokens in MACOS_MODEL_BOUNDARY_REQUIREMENTS.items():
        path = root / relative
        if not path.is_file():
            errors.append(f'missing macOS model boundary source: {relative}')
            continue
        text = path.read_text(encoding='utf-8')
        for token in required_tokens:
            if token not in text:
                errors.append(f'{relative} missing self-hosted model boundary token: {token}')

    source_roots = (
        root / 'desktop' / 'macos' / 'Desktop' / 'Sources',
        root / 'desktop' / 'context-for-claude' / 'Sources',
    )
    for sources in source_roots:
        if not sources.is_dir():
            continue
        for path in sources.rglob('*.swift'):
            network_literal_lines = [
                line.lower()
                for line in path.read_text(encoding='utf-8').splitlines()
                if 'URL(string:' in line or 'URLComponents(string:' in line or 'let prefix =' in line
            ]
            matched = sorted(
                host for host in DIRECT_MODEL_VENDOR_HOSTS if any(host in line for line in network_literal_lines)
            )
            if not matched:
                continue
            relative = path.relative_to(root).as_posix()
            if relative not in MACOS_DIRECT_MODEL_HOST_ALLOWLIST:
                errors.append(f'{relative} adds unreviewed client-direct model host(s): {", ".join(matched)}')
    return errors


def _validate_url(name: str, value: str, errors: list[str]) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.netloc:
        errors.append(f'{name} must be an explicit https URL')
        return
    host = (parsed.hostname or '').lower()
    if any(host == forbidden or host.endswith(f'.{forbidden}') for forbidden in FORBIDDEN_ENDPOINT_HOSTS):
        errors.append(f'{name} must not use official endpoint host {host}')


def validate(compose_path: Path, env_path: Path) -> list[str]:
    errors: list[str] = []
    errors.extend(validate_macos_client_model_egress())
    text = compose_path.read_text(encoding='utf-8')
    backend_dockerfile = DEFAULT_BACKEND_DOCKERFILE.read_text(encoding='utf-8')
    if 'mkdir -p /app/syncing' not in backend_dockerfile:
        errors.append(
            'backend image must pre-create /app/syncing before dropping privileges so its named volume is writable'
        )
    services = _service_blocks(text)
    missing_services = sorted(REQUIRED_SERVICES - services.keys())
    if missing_services:
        errors.append(f'missing required services: {", ".join(missing_services)}')

    for service in sorted(REQUIRED_SERVICES & services.keys()):
        block = services[service]
        if service not in ONE_SHOT_SERVICES and '\n    healthcheck:' not in block:
            errors.append(f'{service} must define a healthcheck')
        image_match = re.search(r'(?m)^    image:\s*(\S+)', block)
        if image_match and image_match.group(1).endswith(':latest'):
            errors.append(f'{service} image must be pinned, not :latest')
        if (
            service in PINNED_REMOTE_SERVICES
            and image_match
            and not re.search(r'@sha256:[0-9a-f]{64}$', image_match.group(1))
        ):
            errors.append(f'{service} image must be pinned by sha256 digest')

    for service, mount_path in STATEFUL_MOUNTS.items():
        block = services.get(service, '')
        if not re.search(rf'(?m)^      - [^\n]+:{re.escape(mount_path)}(?::ro)?$', block):
            errors.append(f'{service} must persist {mount_path}')

    for service in ('auth-migrate', 'auth-server', 'backend', 'queue-worker'):
        if '\n    build:' not in services.get(service, ''):
            errors.append(f'{service} must be buildable from this checkout')
    for service in ('backend', 'queue-worker'):
        if 'platform: ${BACKEND_PLATFORM:?BACKEND_PLATFORM is required}' not in services.get(service, ''):
            errors.append(f'{service} must require an explicit runtime platform')

    backend_env = _environment(services.get('backend', ''))
    for name, expected in REQUIRED_FIXED_BACKEND_ENV.items():
        if backend_env.get(name) != expected:
            errors.append(f'backend {name} must be literal {expected!r}')
    if backend_env.get('AUTH_JWKS_URL') != 'http://auth-server:3000/api/auth/jwks':
        errors.append('backend AUTH_JWKS_URL must use the private auth-server service endpoint')
    if backend_env.get('AUTH_SERVER_INTERNAL_URL') != 'http://auth-server:3000':
        errors.append('backend AUTH_SERVER_INTERNAL_URL must use the private auth-server service endpoint')
    for name in ('AUTH_JWT_ISSUER', 'AUTH_JWT_AUDIENCE'):
        if backend_env.get(name) != '${PUBLIC_AUTH_URL:?PUBLIC_AUTH_URL is required}':
            errors.append(f'backend {name} must use the same PUBLIC_AUTH_URL origin')
    projection_bindings = {
        'VECTOR_PROJECTION_MODE': '${VECTOR_PROJECTION_MODE:?VECTOR_PROJECTION_MODE is required}',
        'VECTOR_PROJECTION_ACTIVE_VERSION': (
            '${VECTOR_PROJECTION_ACTIVE_VERSION:?VECTOR_PROJECTION_ACTIVE_VERSION is required}'
        ),
        'VECTOR_PROJECTION_TARGET_VERSION': '${VECTOR_PROJECTION_TARGET_VERSION-}',
        'VECTOR_PROJECTION_SCHEMA_VERSION': (
            '${VECTOR_PROJECTION_SCHEMA_VERSION:?VECTOR_PROJECTION_SCHEMA_VERSION is required}'
        ),
        'VECTOR_PROJECTION_DELETE_VERSIONS': (
            '${VECTOR_PROJECTION_DELETE_VERSIONS:?VECTOR_PROJECTION_DELETE_VERSIONS is required}'
        ),
        'VECTOR_PROJECTION_REQUIRED_NAMESPACES': (
            '${VECTOR_PROJECTION_REQUIRED_NAMESPACES:?VECTOR_PROJECTION_REQUIRED_NAMESPACES is required}'
        ),
    }
    for name, expected in projection_bindings.items():
        if backend_env.get(name) != expected:
            errors.append(f'backend {name} must use exact projection binding {expected!r}')

    migrate_block = services.get('auth-migrate', '')
    if 'command: ["node", "src/migrate.js"]' not in migrate_block:
        errors.append('auth-migrate must run the explicit Better Auth schema migrator')
    auth_block = services.get('auth-server', '')
    if not re.search(
        r'(?ms)^    depends_on:\s*\n      auth-migrate:\s*\n        condition: service_completed_successfully',
        auth_block,
    ):
        errors.append('auth-server must fail closed behind successful auth-migrate completion')

    for service, required_names in REQUIRED_INTERPOLATED_ENV.items():
        env = _environment(services.get(service, ''))
        for name in sorted(required_names):
            value = env.get(name)
            if value is None:
                errors.append(f'{service} missing required environment {name}')
            elif not _required_interpolation(value):
                errors.append(f'{service} {name} must use required ${{VAR:?message}} interpolation')

    all_env = {name: value for block in services.values() for name, value in _environment(block).items()}
    forbidden_names = sorted(
        name
        for name in all_env
        if name in FORBIDDEN_ENV_NAMES or any(name.startswith(prefix) for prefix in FORBIDDEN_ENV_PREFIXES)
    )
    if forbidden_names:
        errors.append(f'zero-vendor profile contains forbidden environment: {", ".join(forbidden_names)}')

    lowered = text.lower()
    for host in FORBIDDEN_ENDPOINT_HOSTS:
        if host in lowered:
            errors.append(f'zero-vendor profile contains forbidden official endpoint: {host}')
    if 'gcr.io/' in lowered or 'pkg.dev/' in lowered:
        errors.append('zero-vendor profile must not use GCP-hosted image defaults')

    env = _dotenv(env_path)
    example_mode = env_path.name.endswith('.example')
    missing_env = sorted(name for name in REQUIRED_ENV_FILE_KEYS if not env.get(name))
    if missing_env:
        errors.append(f'{env_path.name} missing required values: {", ".join(missing_env)}')
    if 'VECTOR_PROJECTION_TARGET_VERSION' not in env:
        errors.append(f'{env_path.name} must declare VECTOR_PROJECTION_TARGET_VERSION, blank in single mode')
    forbidden_env = sorted(
        name
        for name, value in env.items()
        if value and (name in FORBIDDEN_ENV_NAMES or any(name.startswith(prefix) for prefix in FORBIDDEN_ENV_PREFIXES))
    )
    if forbidden_env:
        errors.append(f'{env_path.name} contains forbidden vendor settings: {", ".join(forbidden_env)}')
    if not example_mode:
        placeholders = sorted(name for name, value in env.items() if 'REPLACE_' in value)
        if placeholders:
            errors.append(f'{env_path.name} contains unreplaced placeholders: {", ".join(placeholders)}')
    for name in ('PUBLIC_BACKEND_URL', 'PUBLIC_AUTH_URL', 'PUBLIC_OBJECTS_URL'):
        if env.get(name):
            _validate_url(name, env[name], errors)
            host = (urlsplit(env[name]).hostname or '').lower()
            if not example_mode and (host == 'example.com' or host.endswith('.example.com')):
                errors.append(f'{name} must not use the reserved example.com deployment host')
    generic_url = env.get('GENERIC_OPENAI_BASE_URL', '')
    if generic_url:
        parsed = urlsplit(generic_url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            errors.append('GENERIC_OPENAI_BASE_URL must be an explicit http(s) URL')
        host = (parsed.hostname or '').lower()
        if any(host == forbidden or host.endswith(f'.{forbidden}') for forbidden in FORBIDDEN_ENDPOINT_HOSTS):
            errors.append(f'GENERIC_OPENAI_BASE_URL must not use official endpoint host {host}')

    for name in ('AUTH_JWKS_ROTATION_SECONDS', 'AUTH_JWKS_GRACE_SECONDS'):
        try:
            seconds = int(env.get(name, ''))
        except ValueError:
            errors.append(f'{name} must be an integer number of seconds')
            continue
        if seconds < 900:
            errors.append(f'{name} must be at least the 15 minute JWT lifetime')

    projection_mode = env.get('VECTOR_PROJECTION_MODE', '')
    active_version = env.get('VECTOR_PROJECTION_ACTIVE_VERSION', '')
    target_version = env.get('VECTOR_PROJECTION_TARGET_VERSION', '')
    projection_version_pattern = re.compile(r'[a-z0-9][a-z0-9_-]*')
    if projection_mode not in {'single', 'dual_write'}:
        errors.append('VECTOR_PROJECTION_MODE must be single or dual_write')
    if not projection_version_pattern.fullmatch(active_version):
        errors.append('VECTOR_PROJECTION_ACTIVE_VERSION must match [a-z0-9][a-z0-9_-]*')
    if target_version and not projection_version_pattern.fullmatch(target_version):
        errors.append('VECTOR_PROJECTION_TARGET_VERSION must match [a-z0-9][a-z0-9_-]* when set')
    if projection_mode == 'single' and target_version:
        errors.append('VECTOR_PROJECTION_TARGET_VERSION must be blank in single mode')
    if projection_mode == 'dual_write' and (not target_version or target_version == active_version):
        errors.append('dual_write requires a distinct VECTOR_PROJECTION_TARGET_VERSION')
    try:
        projection_schema_version = int(env.get('VECTOR_PROJECTION_SCHEMA_VERSION', ''))
    except ValueError:
        projection_schema_version = 0
    if projection_schema_version < 1:
        errors.append('VECTOR_PROJECTION_SCHEMA_VERSION must be a positive integer')
    delete_versions_raw = env.get('VECTOR_PROJECTION_DELETE_VERSIONS', '')
    delete_versions = {value.strip() for value in delete_versions_raw.split(',') if value.strip()}
    if any(not projection_version_pattern.fullmatch(value) for value in delete_versions):
        errors.append('VECTOR_PROJECTION_DELETE_VERSIONS contains an invalid version')
    retained_versions = {active_version, target_version} - {''}
    if not retained_versions.issubset(delete_versions):
        errors.append('VECTOR_PROJECTION_DELETE_VERSIONS must retain every active/target version')
    required_namespaces_raw = env.get('VECTOR_PROJECTION_REQUIRED_NAMESPACES', '')
    required_namespaces_list = [value.strip() for value in required_namespaces_raw.split(',')]
    required_namespaces = {value for value in required_namespaces_list if value}
    if any(not value for value in required_namespaces_list) or len(required_namespaces) != len(
        required_namespaces_list
    ):
        errors.append('VECTOR_PROJECTION_REQUIRED_NAMESPACES must be a duplicate-free namespace list')
    if required_namespaces != REQUIRED_PROJECTION_NAMESPACES:
        errors.append('VECTOR_PROJECTION_REQUIRED_NAMESPACES must cover every self-host vector namespace')

    if not example_mode:
        model_dir = Path(env.get('SENSEVOICE_MODEL_HOST_PATH', ''))
        if not model_dir.is_absolute() or not model_dir.is_dir():
            errors.append('SENSEVOICE_MODEL_HOST_PATH must be an existing absolute directory')
        else:
            missing_model_files = [
                name for name in ('model.int8.onnx', 'tokens.txt') if not (model_dir / name).is_file()
            ]
            if missing_model_files:
                errors.append('SENSEVOICE_MODEL_HOST_PATH is missing required files: ' + ', '.join(missing_model_files))

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose', type=Path, default=DEFAULT_COMPOSE)
    parser.add_argument('--env-file', type=Path, default=DEFAULT_EXAMPLE_ENV)
    args = parser.parse_args(argv)
    errors = validate(args.compose.resolve(), args.env_file.resolve())
    if errors:
        for error in errors:
            print(f'ERROR: {error}', file=sys.stderr)
        return 1
    print(f'self-host deployment contract OK: {args.compose}')
    print('zero-vendor contract OK: Firebase/OpenAI/Pinecone/GCP bindings and official endpoint fallbacks are absent')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
