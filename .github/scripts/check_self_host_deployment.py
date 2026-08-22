#!/usr/bin/env python3
"""Static production/zero-vendor contract for the self-host Compose profile."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMPOSE = ROOT / 'deploy' / 'self-host' / 'compose.production.yml'
DEFAULT_EXAMPLE_ENV = ROOT / 'deploy' / 'self-host' / '.env.production.example'
DEFAULT_BACKEND_DOCKERFILE = ROOT / 'backend' / 'Dockerfile'
DEFAULT_AUTH_DOCKERFILE = ROOT / 'auth-server' / 'Dockerfile'
DEFAULT_SEARXNG_SETTINGS = ROOT / 'deploy' / 'self-host' / 'searxng-settings.yml'
DEFAULT_OPERATIONS = ROOT / 'deploy' / 'self-host' / 'operations.sh'
DEFAULT_SNAPSHOT_TOOL = ROOT / 'deploy' / 'self-host' / 'volume-snapshot.py'

REQUIRED_SERVICES = {
    'postgres',
    'redis',
    'minio',
    'qdrant',
    'typesense',
    'searxng',
    'auth-migrate',
    'firestore-pg-migrate',
    'auth-server',
    'backend',
    'queue-worker',
}
ONE_SHOT_SERVICES = {'auth-migrate', 'firestore-pg-migrate'}
PINNED_REMOTE_SERVICES = {'postgres', 'redis', 'minio', 'qdrant', 'typesense', 'searxng'}
STATEFUL_MOUNTS = {
    'postgres': '/var/lib/postgresql/data',
    'redis': '/data',
    'minio': '/data',
    'qdrant': '/qdrant/storage',
    'typesense': '/data',
    'searxng': '/var/cache/searxng',
    'backend': '/app/syncing',
}
REQUIRED_FIXED_BACKEND_ENV = {
    'OMI_ENV_STAGE': 'prod',
    'OMI_DEPLOYMENT_PROFILE': 'self_hosted',
    'AUTH_PROVIDER': 'better_auth',
    'AGENT_VM_PROVIDER': 'disabled',
    'AUTH_INTERNAL_ALLOW_HTTP': 'true',
    'QUEUE_BACKEND': 'redis',
    'STORAGE_BACKEND': 'minio',
    'VECTOR_STORE_PROVIDER': 'qdrant',
    'MEMORY_KEYWORD_INDEX_PROVIDER': 'typesense',
    'CONVERSATION_KEYWORD_INDEX_PROVIDER': 'typesense',
    'TYPESENSE_HOST': 'typesense',
    'TYPESENSE_HOST_PORT': '8108',
    'TYPESENSE_PROTOCOL': 'http',
    'MEMORY_TYPESENSE_COLLECTION': 'canonical_memory_atoms',
    'CONVERSATION_TYPESENSE_COLLECTION': 'omi_conversations',
    'OMI_LLM_DEFAULT_PROVIDER': 'generic',
    'OMI_LLM_DEFAULT_FALLBACKS': '',
    'TRANSLATION_PROVIDER': 'generic',
    'EMBEDDING_PROVIDER': 'generic',
    'FILE_CHAT_TRANSPORT': 'local_extraction',
    # The checked-in profile remains disabled, while an operator may opt into
    # the separately validated HTTPS webhook through the reviewed env file.
    'PUSH_PROVIDER': '${PUSH_PROVIDER:-disabled}',
    'DESKTOP_VENDOR_PROXY_TRANSPORT': 'disabled',
    'EMBEDDING_CAPABILITY_TRANSPORT': 'direct',
    'PROACTIVE_TOOL_TRANSPORT': 'completion',
    'SPEAKER_EMBEDDING_PROVIDER': 'sherpa_onnx',
    'SPEAKER_EMBEDDING_MODEL': '/models/speaker/speaker.onnx',
    'TTS_SHERPA_MODEL': '/models/tts/model.onnx',
    'TTS_SHERPA_TOKENS': '/models/tts/tokens.txt',
    'TTS_SHERPA_DATA_DIR': '/models/tts/espeak-ng-data',
    'STT_SERVICE_MODELS': 'sensevoice',
    'STT_ROUTE_FALLBACK_TO_DEFAULT': 'false',
    'STT_PRERECORDED_MODEL': 'mlx_moss_diarize',
    'REALTIME_PROVIDER': 'relay',
    'WEB_SEARCH_TRANSPORT': 'searxng',
    'SEARXNG_BASE_URL': 'http://searxng:8080',
    'FIRMWARE_RELEASE_TRANSPORT': 'manifest',
    'DESKTOP_UPDATE_LEGACY_FALLBACK': 'disabled',
    'MEMORY_ENABLED': 'on',
    'ADMIN_KEY_AUTH_ENABLED': 'false',
}
REQUIRED_INTERPOLATED_ENV = {
    'backend': {
        'BASE_API_URL',
        'API_BASE_URL',
        'OMI_SHARE_BASE_URL',
        'SELF_HOST_EGRESS_ALLOWLIST',
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
        'APP_ICON_GENERATION_TRANSPORT',
        'TTS_PROVIDER',
        'TTS_SHERPA_NUM_THREADS',
        'TTS_SHERPA_SPEAKER_ID',
        'SPEAKER_EMBEDDING_NUM_THREADS',
        'MLX_MOSS_DIARIZE_ENDPOINT',
        'MLX_MOSS_DIARIZE_MODEL',
        'GENERIC_OPENAI_BASE_URL',
        'GENERIC_OPENAI_API_KEY',
        'GENERIC_OPENAI_MODEL',
        'EMBEDDING_MODEL',
        'EMBEDDING_DIMENSION',
        'REALTIME_MODEL',
        'REALTIME_RELAY_URL',
        'REALTIME_RELAY_API_KEY',
        'REALTIME_RELAY_PROVIDER_ID',
        'REALTIME_RELAY_WIRE_PROTOCOL',
        'REALTIME_RELAY_ALLOWED_HOSTS',
        'REALTIME_RELAY_MAX_MESSAGE_BYTES',
        'REALTIME_RELAY_MAX_SESSION_SECONDS',
        'MEMORY_V3_CURSOR_SECRET',
        'VECTOR_PROJECTION_MODE',
        'VECTOR_PROJECTION_ACTIVE_VERSION',
        'VECTOR_PROJECTION_SCHEMA_VERSION',
        'VECTOR_PROJECTION_DELETE_VERSIONS',
        'QDRANT_API_KEY',
        'TYPESENSE_API_KEY',
        'FIRMWARE_RELEASE_MANIFEST_URL',
        'FIRMWARE_RELEASE_ASSET_ORIGIN',
        'MCP_AUTHORIZATION_SERVER_URL',
        'MCP_RESOURCE_URL',
    },
    'searxng': {'SEARXNG_SECRET'},
    'typesense': {'TYPESENSE_API_KEY'},
    'queue-worker': {'SELF_HOST_EGRESS_ALLOWLIST'},
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
    'firestore-pg-migrate': {'FIRESTORE_PG_DSN'},
}
REQUIRED_ENV_FILE_KEYS = {
    'SELF_HOST_BIND_ADDRESS',
    'BACKEND_PORT',
    'AUTH_SERVER_PORT',
    'MINIO_API_PORT',
    'MINIO_CONSOLE_PORT',
    'PUBLIC_BACKEND_URL',
    'PUBLIC_AUTH_URL',
    'PUBLIC_MCP_URL',
    'PUBLIC_OBJECTS_URL',
    'OMI_SHARE_BASE_URL',
    'SELF_HOST_EGRESS_ALLOWLIST',
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
    'TYPESENSE_API_KEY',
    'SEARXNG_SECRET',
    'TTS_PROVIDER',
    'TTS_MODEL_HOST_DIR',
    'TTS_SHERPA_NUM_THREADS',
    'TTS_SHERPA_SPEAKER_ID',
    'APP_ICON_GENERATION_TRANSPORT',
    'FIRMWARE_RELEASE_MANIFEST_URL',
    'FIRMWARE_RELEASE_ASSET_ORIGIN',
    'BETTER_AUTH_SECRET',
    'AUTH_INTERNAL_ADMIN_SECRET',
    'AUTH_JWKS_ROTATION_SECONDS',
    'AUTH_JWKS_GRACE_SECONDS',
    'ENCRYPTION_SECRET',
    'MEMORY_V3_CURSOR_SECRET',
    'QUEUE_REDIS_SYNC_WORKER_SECRET',
    'QUEUE_REDIS_AUDIO_MERGE_WORKER_SECRET',
    'QUEUE_REDIS_ACCOUNT_DELETION_WORKER_SECRET',
    'QUEUE_REDIS_FINALIZATION_WORKER_SECRET',
    'GENERIC_OPENAI_BASE_URL',
    'GENERIC_OPENAI_API_KEY',
    'GENERIC_OPENAI_MODEL',
    'EMBEDDING_MODEL',
    'EMBEDDING_DIMENSION',
    'REALTIME_MODEL',
    'REALTIME_RELAY_URL',
    'REALTIME_RELAY_API_KEY',
    'REALTIME_RELAY_PROVIDER_ID',
    'REALTIME_RELAY_WIRE_PROTOCOL',
    'REALTIME_RELAY_ALLOWED_HOSTS',
    'REALTIME_RELAY_MAX_MESSAGE_BYTES',
    'REALTIME_RELAY_MAX_SESSION_SECONDS',
    'VECTOR_PROJECTION_MODE',
    'VECTOR_PROJECTION_ACTIVE_VERSION',
    'VECTOR_PROJECTION_SCHEMA_VERSION',
    'VECTOR_PROJECTION_DELETE_VERSIONS',
    'VECTOR_PROJECTION_REQUIRED_NAMESPACES',
    'SENSEVOICE_MODEL_HOST_PATH',
    'MLX_MOSS_DIARIZE_ENDPOINT',
    'MLX_MOSS_DIARIZE_MODEL',
    'MLX_MOSS_DIARIZE_ACCEPTANCE_WAV_HOST_PATH',
    'SPEAKER_MODEL_HOST_DIR',
    'SPEAKER_EMBEDDING_NUM_THREADS',
}
DECLARED_OPTIONAL_ENV_FILE_KEYS = {
    'DESKTOP_UPDATE_DOWNLOAD_URL',
    'TTS_OPENAI_COMPATIBLE_BASE_URL',
    'TTS_OPENAI_COMPATIBLE_API_KEY',
    'TTS_OPENAI_COMPATIBLE_MODEL',
    'TTS_OPENAI_COMPATIBLE_VOICE',
    'IMAGE_GENERATION_OPENAI_COMPATIBLE_BASE_URL',
    'IMAGE_GENERATION_OPENAI_COMPATIBLE_API_KEY',
    'IMAGE_GENERATION_OPENAI_COMPATIBLE_MODEL',
    'FIRMWARE_RELEASE_MANIFEST_BEARER_TOKEN',
    'MLX_MOSS_DIARIZE_API_KEY',
}
OPTIONAL_BACKEND_ENV_BINDINGS = {name: f'${{{name}-}}' for name in DECLARED_OPTIONAL_ENV_FILE_KEYS}
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
    'anthropic.com',
    'deepgram.com',
    'hume.ai',
    'langchain.com',
    'langsmith.com',
    'posthog.com',
    'sentry.io',
    'xiaomimimo.com',
    'mosi.cn',
)


def _private_endpoint_host(host: str) -> bool:
    if host == 'localhost' or '.' not in host or host.endswith(('.internal', '.svc', '.svc.cluster.local')):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    networks = (
        ipaddress.ip_network('10.0.0.0/8'),
        ipaddress.ip_network('172.16.0.0/12'),
        ipaddress.ip_network('192.168.0.0/16'),
        ipaddress.ip_network('fc00::/7'),
    )
    return any(address in network for network in networks)


def _unsafe_endpoint_host(host: str) -> bool:
    if host in {'metadata', 'metadata.google.internal'}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_link_local or address.is_unspecified or address.is_multicast or address.is_reserved


def _validate_operator_http_endpoint(name: str, value: str, errors: list[str]) -> None:
    parsed = urlsplit(value)
    host = (parsed.hostname or '').lower()
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.netloc
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        errors.append(f'{name} must be an explicit credential-free http(s) base URL')
        return
    if _unsafe_endpoint_host(host):
        errors.append(f'{name} must not target link-local, metadata, or reserved hosts')
    if parsed.scheme == 'http' and not _private_endpoint_host(host):
        errors.append(f'{name} must use https for a public target host')
    if any(host == forbidden or host.endswith(f'.{forbidden}') for forbidden in FORBIDDEN_ENDPOINT_HOSTS):
        errors.append(f'{name} must not use official endpoint host {host}')


def _validate_operator_download_url(name: str, value: str, errors: list[str]) -> None:
    """Validate a manual installer URL without requiring a base-URL shape."""

    parsed = urlsplit(value)
    host = (parsed.hostname or '').lower()
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.netloc
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        errors.append(f'{name} must be an explicit credential-free http(s) URL')
        return
    if _unsafe_endpoint_host(host):
        errors.append(f'{name} must not target link-local, metadata, or reserved hosts')
    if parsed.scheme == 'http' and not _private_endpoint_host(host):
        errors.append(f'{name} must use https for a public target host')
    if any(host == forbidden or host.endswith(f'.{forbidden}') for forbidden in FORBIDDEN_ENDPOINT_HOSTS):
        errors.append(f'{name} must not use official endpoint host {host}')


def _egress_allowlist_contains(host: str, allowlist: set[str]) -> bool:
    normalized = host.lower().rstrip('.')
    return normalized in allowlist or any(
        value.startswith('*.') and normalized.endswith(f'.{value[2:]}') for value in allowlist
    )


def _validate_egress_allowlist(env: dict[str, str], errors: list[str]) -> None:
    """Validate the runtime HTTP authority declaration and known targets."""

    raw = env.get('SELF_HOST_EGRESS_ALLOWLIST', '')
    allowlist = {value.strip().lower().rstrip('.') for value in raw.split(',') if value.strip()}
    if not allowlist:
        errors.append('SELF_HOST_EGRESS_ALLOWLIST must declare at least one external host')
        return
    for value in sorted(allowlist):
        host = value[2:] if value.startswith('*.') else value
        if value == '*' or not host or '/' in host or ':' in host or '://' in host:
            errors.append(f'SELF_HOST_EGRESS_ALLOWLIST contains an invalid host token: {value}')
        if any(host == forbidden or host.endswith(f'.{forbidden}') for forbidden in FORBIDDEN_ENDPOINT_HOSTS):
            errors.append(f'SELF_HOST_EGRESS_ALLOWLIST must not contain official endpoint host {host}')

    known_targets = {
        name: (urlsplit(value).hostname or '').lower()
        for name, value in (
            ('GENERIC_OPENAI_BASE_URL', env.get('GENERIC_OPENAI_BASE_URL', '')),
            ('REALTIME_RELAY_URL', env.get('REALTIME_RELAY_URL', '')),
            ('FIRMWARE_RELEASE_MANIFEST_URL', env.get('FIRMWARE_RELEASE_MANIFEST_URL', '')),
            ('TTS_OPENAI_COMPATIBLE_BASE_URL', env.get('TTS_OPENAI_COMPATIBLE_BASE_URL', '')),
            ('IMAGE_GENERATION_OPENAI_COMPATIBLE_BASE_URL', env.get('IMAGE_GENERATION_OPENAI_COMPATIBLE_BASE_URL', '')),
            ('PUSH_WEBHOOK_URL', env.get('PUSH_WEBHOOK_URL', '')),
            ('MLX_MOSS_DIARIZE_ENDPOINT', env.get('MLX_MOSS_DIARIZE_ENDPOINT', '')),
        )
        if value
    }
    internal_hosts = {'localhost', '127.0.0.1', '::1', 'host.docker.internal'}
    for name, host in known_targets.items():
        if host and host in internal_hosts:
            continue
        if host and not _egress_allowlist_contains(host, allowlist):
            errors.append(f'SELF_HOST_EGRESS_ALLOWLIST must contain {name} host {host}')


MACOS_MODEL_BOUNDARY_REQUIREMENTS = {
    'desktop/macos/Desktop/Sources/DesktopBackendEnvironment.swift': (
        'canonicalSelfHostedOrigin(',
        'components.user == nil',
        'host.hasSuffix(".omi.me")',
        'desktop-backend-hhibjajaja-uc.a.run.app',
        'desktop-backend-dt5lrfkkoa-uc.a.run.app',
    ),
    'desktop/macos/Desktop/Sources/ProactiveAssistants/Core/GeminiClient.swift': (
        'providerNeutralBackendCapability',
        'ProactiveLaneClient.shared.complete',
        'capabilityUnavailable("proactive_tool_calling")',
    ),
    'desktop/macos/Desktop/Sources/ProactiveAssistants/Services/EmbeddingService.swift': (
        'v1/model-capabilities/embeddings',
        '"projection_namespace": purpose.projectionNamespace',
        'expectedLogicalNamespace: purpose.projectionNamespace',
        'ProjectedEmbeddingBatch',
        'embeddingProjectionMatches(',
    ),
    'desktop/macos/Desktop/Sources/Rewind/Core/RewindDatabase+Embeddings.swift': (
        'writeEmbeddingIfProjectionMatches(',
        'projectionKey = ?',
        'updateScreenshotEmbeddingIfProjectionMatches(',
    ),
    'desktop/macos/Desktop/Sources/Rewind/Services/OCREmbeddingService.swift': (
        'embedBatchProjected(',
        'updateScreenshotEmbeddingIfProjectionMatches(',
        'embeddingProjectionMatches(',
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
WINDOWS_MODEL_BOUNDARY_REQUIREMENTS = {
    'desktop/windows/src/shared/deploymentProfile.ts': (
        "profile === 'self_hosted'",
        'allowDirectModelProviders: profile === \'omi_cloud\'',
        'allowByok: profile === \'omi_cloud\'',
        'desktop-backend-dt5lrfkkoa-uc.a.run.app',
        'VITE_OMI_MCP_CHATGPT_OAUTH_CLIENT_ID',
        'VITE_OMI_MCP_CLAUDE_OAUTH_CLIENT_ID',
    ),
    'desktop/windows/scripts/ensure-env.mjs': (
        "requestedProfile === 'self_hosted'",
        'self_hosted artifacts must not contain Firebase configuration',
    ),
    'desktop/windows/src/renderer/src/lib/identity.ts': (
        "deployment.identityProvider === 'firebase'",
        'betterAuthRestore()',
        'result.definitive',
        'scheduleTransientIdentityRefresh',
    ),
    'desktop/windows/src/main/auth/identityToken.ts': (
        "config.identityProvider === 'firebase'",
        "new URL('/api/auth/jwks', config.authBase)",
        'verifyBetterAuthToken',
    ),
    'desktop/windows/src/main/ipc/omiListen.ts': ('backendWebSocketOrigin()',),
    'desktop/windows/src/main/ipc/voiceHub.ts': (
        "wire_protocol !== 'openai_realtime_v1'",
        'Authorization: `Bearer ${session.token}`',
        'candidate.hostname !== websocketBase.hostname',
    ),
    'desktop/windows/src/main/ipc/byok.ts': ('requireByok()',),
    'desktop/windows/src/main/agentKernel/byokValidator.ts': ('resolveWindowsDeployment().allowByok',),
    'desktop/windows/src/main/ipc/codingAgent.ts': ('allowExternalCodingAgents()',),
    'desktop/windows/src/main/agentKernel/controlPlane.ts': ("resolveWindowsDeployment().profile === 'self_hosted'",),
    'desktop/windows/src/renderer/src/lib/voice/voiceController.ts': (
        'resolveWindowsDeployment().allowDirectModelProviders',
    ),
    'desktop/windows/src/renderer/src/lib/voice/hub/hubController.ts': (
        'allowsDirectModelProviders',
        'configured backend relay capability',
    ),
    'desktop/windows/src/renderer/src/lib/voice/hub/openaiHubSession.ts': (
        'resolveWindowsDeployment().allowDirectModelProviders',
    ),
    'desktop/windows/src/renderer/src/lib/voice/hub/geminiHubSession.ts': (
        'resolveWindowsDeployment().allowDirectModelProviders',
    ),
    'desktop/windows/electron.vite.config.ts': (
        'signed-self-hosted-csp',
        'rewriteSelfHostedCsp',
        'transformIndexHtml',
    ),
    'desktop/windows/src/shared/selfHostedCsp.ts': (
        'return url.origin',
        'connect-src ${connect}',
        'exactly one CSP meta element',
    ),
    'desktop/windows/scripts/check-self-host-artifact.mjs': (
        'connect-src',
        'renderer artifact contains forbidden CSP/vendor host',
        'exactly one connect-src directive',
        'connect-src contains unsigned source',
        'VITE_OMI_MCP_CHATGPT_OAUTH_CLIENT_ID',
        'VITE_OMI_MCP_CLAUDE_OAUTH_CLIENT_ID',
    ),
    'desktop/windows/src/main/assistants/core/modelCapabilityClient.ts': (
        '/v1/model-capabilities/tool-completions',
        "feature: 'desktop_proactive_reasoning'",
        'model capability returned an undeclared tool call',
    ),
    'desktop/windows/src/main/assistants/focus/gemini.ts': (
        "profile === 'self_hosted'",
        'completeStructuredCapability',
    ),
    'desktop/windows/src/main/assistants/memory/gemini.ts': (
        "profile === 'self_hosted'",
        'completeStructuredCapability',
    ),
    'desktop/windows/src/main/assistants/goals/generate.ts': (
        "profile === 'self_hosted'",
        'completeStructuredCapability',
    ),
    'desktop/windows/src/main/assistants/insight/gemini.ts': (
        "profile === 'self_hosted'",
        'completeToolCapability',
    ),
    'desktop/windows/src/main/assistants/tasks/geminiWire.ts': (
        "profile === 'self_hosted'",
        'completeToolCapability',
    ),
    'desktop/windows/src/main/ipc/modelCapability.ts': (
        "surface === 'live_notes'",
        'completeStructuredCapability',
        'identity session missing',
    ),
    'desktop/windows/src/renderer/src/lib/geminiClient.ts': (
        "profile === 'self_hosted'",
        'window.omi.modelCapabilityGenerate',
    ),
    'desktop/windows/src/preload/index.ts': ("ipcRenderer.invoke('modelCapability:generate'",),
    'desktop/windows/src/main/mcp/cloudConnectors.ts': (
        'if (clients.chatgpt)',
        'if (clients.claude)',
    ),
    'desktop/windows/src/main/ipc/mcpExports.ts': (
        'deployment.mcpChatgptOAuthClientId',
        'deployment.mcpClaudeOAuthClientId',
    ),
}
CONTEXT_CLIENT_BOUNDARY_REQUIREMENTS = {
    'desktop/context-for-claude/Sources/ContextCore/DeploymentProfile.swift': (
        'OmiDesktopBaseURL',
        'OMI_DESKTOP_API_URL',
        'rejectsManagedOrigin: true',
        'desktop-backend-hhibjajaja-uc.a.run.app',
        'desktop-backend-dt5lrfkkoa-uc.a.run.app',
        'OmiSpeechModelMode',
        'invalidSpeechModelAuthority',
    ),
    'desktop/context-for-claude/Sources/ContextApp/Backend/ScreenActivityUploader.swift': (
        'ContextDeploymentProfile.current.desktopBaseURL',
        'appendingPathComponent("v1/screen-activity/sync")',
    ),
    'desktop/context-for-claude/scripts/build.sh': (
        'CONTEXT_DESKTOP_BASE_URL',
        'self_hosted requires CONTEXT_DESKTOP_BASE_URL',
        'Set :OmiDesktopBaseURL',
        'CONTEXT_SPEECH_MODEL_MODE=local or disabled',
        'Contents/Resources/$SPEECH_MODEL_BUNDLE_PATH',
    ),
    'desktop/context-for-claude/Sources/ContextApp/Transcribe/Transcriber.swift': (
        'speechModelAuthority',
        'ModelHub.offlineMode = true',
        'obtainOperatorProvisioned',
        'SpeechModelError.capabilityUnavailable',
    ),
}
FLUTTER_MODEL_BOUNDARY_REQUIREMENTS = {
    'app/lib/env/env.dart': (
        'OMI_MCP_BASE_URL',
        'resolveMcpBaseUrl',
        'canonicalSelfHostedOrigin',
        "uri.userInfo.isNotEmpty",
    ),
    'app/lib/services/auth_service.dart': (
        'Env.canonicalSelfHostedOrigin(',
        "key: 'OMI_AUTH_SERVER_URL'",
    ),
    'app/lib/models/stt_provider.dart': (
        'isSelfHostedClientSafe',
        'providersForProfile',
        'requestTemplatesForProfile',
    ),
    'app/lib/services/sockets/transcription_service.dart': (
        'validateDeploymentEgress',
        'createTransportForProfile',
        'createTransport: () =>',
    ),
    'app/lib/pages/settings/transcription_settings_page.dart': (
        'providersForProfile(Env.profile)',
        'requestTemplatesForProfile(Env.profile)',
    ),
    'app/lib/pages/onboarding/wrapper.dart': (
        'OnboardingIdentity',
        'OnboardingIdentity.allowsManagedSupport(Env.profile)',
    ),
    'app/lib/utils/analytics/intercom.dart': (
        'displayMessengerForDeployment',
        'display: () => intercom.displayMessenger()',
    ),
}


def validate_release_client_model_egress(root: Path = ROOT) -> list[str]:
    """Static tripwire for Windows/Flutter pre-transport self-host boundaries."""
    errors: list[str] = []
    requirements = {
        **WINDOWS_MODEL_BOUNDARY_REQUIREMENTS,
        **CONTEXT_CLIENT_BOUNDARY_REQUIREMENTS,
        **FLUTTER_MODEL_BOUNDARY_REQUIREMENTS,
    }
    for relative, required_tokens in requirements.items():
        path = root / relative
        if not path.is_file():
            errors.append(f'missing release client model boundary source: {relative}')
            continue
        text = path.read_text(encoding='utf-8')
        for token in required_tokens:
            if token not in text:
                errors.append(f'{relative} missing self-hosted model boundary token: {token}')

    listen = root / 'desktop/windows/src/main/ipc/omiListen.ts'
    if listen.is_file() and 'wss://api.omi.me' in listen.read_text(encoding='utf-8'):
        errors.append('Windows listen transport must derive WebSocket authority from the signed backend origin')

    self_host_env = root / 'desktop/windows/.env.selfhost.example'
    if not self_host_env.is_file():
        errors.append('missing Windows .env.selfhost.example')
    else:
        firebase_keys = re.findall(r'(?m)^VITE_FIREBASE_[A-Z0-9_]+\s*=.+$', self_host_env.read_text(encoding='utf-8'))
        if firebase_keys:
            errors.append('Windows self-host example must not declare Firebase configuration')
    return errors


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
    if parsed.username is not None or parsed.password is not None:
        errors.append(f'{name} must be an origin URL without embedded credentials')
    if parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        errors.append(f'{name} must be an origin URL without path, query, or fragment')
    host = (parsed.hostname or '').lower()
    if any(host == forbidden or host.endswith(f'.{forbidden}') for forbidden in FORBIDDEN_ENDPOINT_HOSTS):
        errors.append(f'{name} must not use official endpoint host {host}')


def validate(compose_path: Path, env_path: Path) -> list[str]:
    errors: list[str] = []
    errors.extend(validate_macos_client_model_egress())
    errors.extend(validate_release_client_model_egress())
    text = compose_path.read_text(encoding='utf-8')
    operations_text = DEFAULT_OPERATIONS.read_text(encoding='utf-8')
    snapshot_text = DEFAULT_SNAPSHOT_TOOL.read_text(encoding='utf-8')
    provider_attestation_text = (ROOT / 'deploy' / 'self-host' / 'runtime_provider_attestation.py').read_text(
        encoding='utf-8'
    )
    operator_evidence_text = (ROOT / 'deploy' / 'self-host' / 'operator_evidence.py').read_text(encoding='utf-8')
    push_model_text = (ROOT / 'backend' / 'models' / 'other.py').read_text(encoding='utf-8')
    if (
        'SCHEMA_VERSION = 2' not in provider_attestation_text
        or "'capability_routes'" not in provider_attestation_text
        or "'roundtrip_scope': 'transport_only'" not in provider_attestation_text
        or "'identity_scope': 'embedding_only'" not in provider_attestation_text
        or "'receipt_schema': 'omi.push.receipt.v1'" not in provider_attestation_text
    ):
        errors.append(
            'runtime provider attestation must enforce the capability route manifest and typed evidence scopes'
        )
    if (
        'CAPABILITY_PROVENANCE_SCHEMA_VERSION = 1' not in operator_evidence_text
        or 'CAPABILITY_NAMES' not in operator_evidence_text
        or 'validate_capability_provenance' not in operator_evidence_text
        or "'generic_llm'" not in operator_evidence_text
        or "'stt_diarization'" not in operator_evidence_text
        or "'speaker_identity'" not in operator_evidence_text
    ):
        errors.append('operator capability provenance must cover every model-backed route')
    if (
        "omi.push.device-token.v1" not in push_model_text
        or "opaque_registered_token" not in push_model_text
        or 'token_type' not in push_model_text
    ):
        errors.append('mobile token registration must use the provider-neutral opaque-token schema')
    if 'SELF_HOST_BACKUP_KEY_FILE' in text:
        errors.append('Compose must not receive the operations-only backup key path')
    if (
        'SELF_HOST_BACKUP_KEY_FILE' not in operations_text
        or '--volume "$key_file:/backup-key/key:ro"' not in operations_text
        or '--key-file /backup-key/key' not in operations_text
    ):
        errors.append('operations.sh must require the explicit backup key file for every backup path')
    if (
        "ENVELOPE_FORMAT = 'omi-backup-aead-v1'" not in snapshot_text
        or 'MANIFEST_SCHEMA_VERSION = 3' not in snapshot_text
    ):
        errors.append('volume-snapshot.py must enforce the authenticated encrypted manifest format')
    searxng_settings = DEFAULT_SEARXNG_SETTINGS.read_text(encoding='utf-8')
    if not re.search(r'(?ms)^search:\s*\n\s+formats:\s*\n(?:\s+-\s+\w+\s*\n)*\s+-\s+json\s*$', searxng_settings):
        errors.append('SearXNG settings must enable the JSON search API')
    if 'secret_key:' in searxng_settings or 'ultrasecretkey' in searxng_settings:
        errors.append('SearXNG settings must receive its secret from required SEARXNG_SECRET injection')
    if not re.search(
        r'(?ms)^use_default_settings:\s*\n\s+engines:\s*\n\s+keep_only:\s*\n\s+- wikipedia\s*$',
        searxng_settings,
    ):
        errors.append('SearXNG outbound engine allowlist must keep only wikipedia')
    backend_dockerfile = DEFAULT_BACKEND_DOCKERFILE.read_text(encoding='utf-8')
    auth_dockerfile = DEFAULT_AUTH_DOCKERFILE.read_text(encoding='utf-8')
    if 'mkdir -p /app/syncing' not in backend_dockerfile:
        errors.append(
            'backend image must pre-create /app/syncing before dropping privileges so its named volume is writable'
        )
    for name, dockerfile in (('backend', backend_dockerfile), ('auth-server', auth_dockerfile)):
        for label in ('com.omi.source.git-commit', 'com.omi.source.git-tree'):
            if label not in dockerfile:
                errors.append(f'{name} image must embed the attributed {label} label')
    services = _service_blocks(text)
    missing_services = sorted(REQUIRED_SERVICES - services.keys())
    if missing_services:
        errors.append(f'missing required services: {", ".join(missing_services)}')

    for service in sorted(REQUIRED_SERVICES & services.keys()):
        block = services[service]
        environment_names = re.findall(r'(?m)^      - ([A-Z][A-Z0-9_]*)=', block)
        duplicate_environment_names = sorted(
            name for name in set(environment_names) if environment_names.count(name) > 1
        )
        if duplicate_environment_names:
            errors.append(f'{service} contains duplicate environment: {", ".join(duplicate_environment_names)}')
        if service not in ONE_SHOT_SERVICES and '\n    healthcheck:' not in block:
            errors.append(f'{service} must define a healthcheck')
        if service == 'backend' and not re.search(
            r'(?s)\n    healthcheck:.*?127\.0\.0\.1:8080/ready(?:["\s]|$)', block
        ):
            errors.append('backend healthcheck must probe the dependency-aware /ready endpoint')
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
        for build_arg in ('OMI_SOURCE_GIT_COMMIT', 'OMI_SOURCE_GIT_TREE'):
            if f'{build_arg}: ${{{build_arg}:-unattributed}}' not in services.get(service, ''):
                errors.append(f'{service} must pass the attributed {build_arg} image build argument')
    for service in ('auth-server', 'backend', 'queue-worker'):
        if 'com.omi.runtime.config-sha256: ${OMI_RUNTIME_CONFIG_SHA256:-unattributed}' not in services.get(service, ''):
            errors.append(f'{service} must bind the reviewed runtime config hash to the exact container')
    for service in ('backend', 'queue-worker'):
        if 'platform: ${BACKEND_PLATFORM:?BACKEND_PLATFORM is required}' not in services.get(service, ''):
            errors.append(f'{service} must require an explicit runtime platform')

    if (
        '${SPEAKER_MODEL_HOST_DIR:?SPEAKER_MODEL_HOST_DIR is required}/speaker.onnx:'
        '/models/speaker/speaker.onnx:ro' not in services.get('backend', '')
    ):
        errors.append('backend must mount the explicit speaker model read-only')
    if '${TTS_MODEL_HOST_DIR:?TTS_MODEL_HOST_DIR is required}:/models/tts:ro' not in services.get('backend', ''):
        errors.append('backend must mount the explicit local TTS model directory read-only')
    if 'host.docker.internal:host-gateway' not in services.get('backend', ''):
        errors.append('backend must map host.docker.internal through the Linux host-gateway boundary')

    backend_env = _environment(services.get('backend', ''))
    for name, expected in REQUIRED_FIXED_BACKEND_ENV.items():
        if backend_env.get(name) != expected:
            errors.append(f'backend {name} must be literal {expected!r}')
    for name, expected in OPTIONAL_BACKEND_ENV_BINDINGS.items():
        if backend_env.get(name) != expected:
            errors.append(f'backend {name} must use exact optional binding {expected!r}')
    if (
        backend_env.get('SELF_HOST_EGRESS_ALLOWLIST')
        != '${SELF_HOST_EGRESS_ALLOWLIST:?SELF_HOST_EGRESS_ALLOWLIST is required}'
    ):
        errors.append('backend SELF_HOST_EGRESS_ALLOWLIST must use the required runtime binding')
    if backend_env.get('AUTH_JWKS_URL') != 'http://auth-server:3000/api/auth/jwks':
        errors.append('backend AUTH_JWKS_URL must use the private auth-server service endpoint')
    if backend_env.get('AUTH_SERVER_INTERNAL_URL') != 'http://auth-server:3000':
        errors.append('backend AUTH_SERVER_INTERNAL_URL must use the private auth-server service endpoint')
    searxng_block = services.get('searxng', '')
    if './searxng-settings.yml:/etc/searxng/settings.yml:ro' not in searxng_block:
        errors.append('searxng must mount the reviewed settings file read-only')
    queue_worker_env = _environment(services.get('queue-worker', ''))
    for name, expected in {
        'WEB_SEARCH_TRANSPORT': 'searxng',
        'SEARXNG_BASE_URL': 'http://searxng:8080',
        'MEMORY_KEYWORD_INDEX_PROVIDER': 'typesense',
        'CONVERSATION_KEYWORD_INDEX_PROVIDER': 'typesense',
        'TYPESENSE_HOST': 'typesense',
        'TYPESENSE_HOST_PORT': '8108',
        'TYPESENSE_PROTOCOL': 'http',
        'MEMORY_TYPESENSE_COLLECTION': 'canonical_memory_atoms',
        'CONVERSATION_TYPESENSE_COLLECTION': 'omi_conversations',
        'PUSH_PROVIDER': '${PUSH_PROVIDER:-disabled}',
    }.items():
        if queue_worker_env.get(name) != expected:
            errors.append(f'queue-worker {name} must be literal {expected!r}')
    if (
        queue_worker_env.get('SELF_HOST_EGRESS_ALLOWLIST')
        != '${SELF_HOST_EGRESS_ALLOWLIST:?SELF_HOST_EGRESS_ALLOWLIST is required}'
    ):
        errors.append('queue-worker SELF_HOST_EGRESS_ALLOWLIST must use the required runtime binding')
    push_webhook_bindings = {
        'PUSH_WEBHOOK_URL': '${PUSH_WEBHOOK_URL-}',
        'PUSH_WEBHOOK_SECRET_FILE': '${PUSH_WEBHOOK_SECRET_FILE-}',
        'PUSH_WEBHOOK_TIMEOUT_SECONDS': '${PUSH_WEBHOOK_TIMEOUT_SECONDS:-5}',
        'PUSH_WEBHOOK_MAX_ATTEMPTS': '${PUSH_WEBHOOK_MAX_ATTEMPTS:-3}',
    }
    for service in ('backend', 'queue-worker'):
        service_env = _environment(services.get(service, ''))
        for name, expected in push_webhook_bindings.items():
            if service_env.get(name) != expected:
                errors.append(f'{service} {name} must use exact optional binding {expected!r}')
        if 'PUSH_WEBHOOK_SECRET=' in services.get(service, ''):
            errors.append(f'{service} must not accept an inline PUSH_WEBHOOK_SECRET')
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

    firestore_migrate_block = services.get('firestore-pg-migrate', '')
    if 'command: ["python", "scripts/firestore_pg_migrate.py", "migrate"]' not in firestore_migrate_block:
        errors.append('firestore-pg-migrate must run the explicit forward-only schema owner')
    for service in ('backend', 'queue-worker'):
        if not re.search(
            r'(?ms)^    depends_on:\s*\n(?:.*?\n)*?      firestore-pg-migrate:\s*\n'
            r'        condition: service_completed_successfully',
            services.get(service, ''),
        ):
            errors.append(f'{service} must fail closed behind successful firestore-pg-migrate completion')

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
    _validate_egress_allowlist(env, errors)
    if 'SELF_HOST_BACKUP_KEY_FILE' in env:
        errors.append('SELF_HOST_BACKUP_KEY_FILE is operations-only and must not be stored in the Compose env file')
    if env.get('BACKEND_IMAGE') and env.get('BACKEND_IMAGE') == env.get('AUTH_SERVER_IMAGE'):
        errors.append('BACKEND_IMAGE and AUTH_SERVER_IMAGE must be distinct tags')
    missing_env = sorted(name for name in REQUIRED_ENV_FILE_KEYS if not env.get(name))
    if missing_env:
        errors.append(f'{env_path.name} missing required values: {", ".join(missing_env)}')
    missing_declarations = sorted(name for name in DECLARED_OPTIONAL_ENV_FILE_KEYS if name not in env)
    if missing_declarations:
        errors.append(f'{env_path.name} missing optional capability declarations: {", ".join(missing_declarations)}')
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
    for name in ('PUBLIC_BACKEND_URL', 'PUBLIC_AUTH_URL', 'PUBLIC_MCP_URL', 'PUBLIC_OBJECTS_URL'):
        if env.get(name):
            _validate_url(name, env[name], errors)
            host = (urlsplit(env[name]).hostname or '').lower()
            if not example_mode and (host == 'example.com' or host.endswith('.example.com')):
                errors.append(f'{name} must not use the reserved example.com deployment host')
    share_origin = env.get('OMI_SHARE_BASE_URL', '')
    if share_origin:
        _validate_url('OMI_SHARE_BASE_URL', share_origin, errors)
        share_host = (urlsplit(share_origin).hostname or '').lower().rstrip('.')
        if (
            share_host == 'omi.me'
            or share_host.endswith('.omi.me')
            or share_host == 'omiapi.com'
            or share_host.endswith('.omiapi.com')
        ):
            errors.append('OMI_SHARE_BASE_URL must use an operator-owned host, not an Omi-operated host')
        if not example_mode and (share_host == 'example.com' or share_host.endswith('.example.com')):
            errors.append('OMI_SHARE_BASE_URL must not use the reserved example.com deployment host')
    generic_url = env.get('GENERIC_OPENAI_BASE_URL', '')
    if generic_url:
        parsed = urlsplit(generic_url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            errors.append('GENERIC_OPENAI_BASE_URL must be an explicit http(s) URL')
        host = (parsed.hostname or '').lower()
        if any(host == forbidden or host.endswith(f'.{forbidden}') for forbidden in FORBIDDEN_ENDPOINT_HOSTS):
            errors.append(f'GENERIC_OPENAI_BASE_URL must not use official endpoint host {host}')

    desktop_download_url = env.get('DESKTOP_UPDATE_DOWNLOAD_URL', '')
    if desktop_download_url:
        _validate_operator_download_url('DESKTOP_UPDATE_DOWNLOAD_URL', desktop_download_url, errors)

    mlx_moss_endpoint = env.get('MLX_MOSS_DIARIZE_ENDPOINT', '')
    if mlx_moss_endpoint:
        _validate_operator_http_endpoint('MLX_MOSS_DIARIZE_ENDPOINT', mlx_moss_endpoint, errors)
        parsed = urlsplit(mlx_moss_endpoint)
        host = (parsed.hostname or '').lower()
        if parsed.path != '/v1/audio/transcriptions':
            errors.append('MLX_MOSS_DIARIZE_ENDPOINT must use exact path /v1/audio/transcriptions')
        if host == 'mosi.cn' or host.endswith('.mosi.cn'):
            errors.append(f'MLX_MOSS_DIARIZE_ENDPOINT must not use official hosted MOSS host {host}')
        if host == 'omi.me' or host.endswith('.omi.me'):
            errors.append(f'MLX_MOSS_DIARIZE_ENDPOINT must not use Omi-operated host {host}')
        if (
            parsed.scheme == 'https'
            and host
            and not _private_endpoint_host(host)
            and not env.get('MLX_MOSS_DIARIZE_API_KEY')
        ):
            errors.append('public HTTPS MLX_MOSS_DIARIZE_ENDPOINT requires MLX_MOSS_DIARIZE_API_KEY')

    realtime_url = env.get('REALTIME_RELAY_URL', '')
    if realtime_url:
        parsed = urlsplit(realtime_url)
        if parsed.scheme not in {'ws', 'wss'} or not parsed.netloc:
            errors.append('REALTIME_RELAY_URL must be an explicit ws(s) URL')
        realtime_host = (parsed.hostname or '').lower()
        if realtime_host and _unsafe_endpoint_host(realtime_host):
            errors.append('REALTIME_RELAY_URL must not target link-local, metadata, or reserved hosts')
        if parsed.scheme == 'ws' and realtime_host and not _private_endpoint_host(realtime_host):
            errors.append('REALTIME_RELAY_URL must use wss for a public target host')
        if any(
            realtime_host == forbidden or realtime_host.endswith(f'.{forbidden}')
            for forbidden in FORBIDDEN_ENDPOINT_HOSTS
        ):
            errors.append(f'REALTIME_RELAY_URL must not use official endpoint host {realtime_host}')
        allowed_hosts = {
            value.strip().lower() for value in env.get('REALTIME_RELAY_ALLOWED_HOSTS', '').split(',') if value.strip()
        }
        if realtime_host not in allowed_hosts:
            errors.append('REALTIME_RELAY_ALLOWED_HOSTS must contain the exact REALTIME_RELAY_URL host')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', env.get('REALTIME_RELAY_PROVIDER_ID', '')):
        errors.append('REALTIME_RELAY_PROVIDER_ID must be a lowercase provider identifier')
    if env.get('REALTIME_RELAY_WIRE_PROTOCOL', '') != 'openai_realtime_v1':
        errors.append('REALTIME_RELAY_WIRE_PROTOCOL must be openai_realtime_v1')
    tts_provider = env.get('TTS_PROVIDER', '')
    tts_settings = (
        'TTS_OPENAI_COMPATIBLE_BASE_URL',
        'TTS_OPENAI_COMPATIBLE_API_KEY',
        'TTS_OPENAI_COMPATIBLE_MODEL',
        'TTS_OPENAI_COMPATIBLE_VOICE',
    )
    if tts_provider not in {'sherpa_onnx', 'openai_compatible'}:
        errors.append('TTS_PROVIDER must be sherpa_onnx or openai_compatible')
    elif tts_provider == 'openai_compatible':
        missing_tts = sorted(name for name in tts_settings if not env.get(name))
        if missing_tts:
            errors.append(f'openai_compatible TTS requires: {", ".join(missing_tts)}')
        elif env.get('TTS_OPENAI_COMPATIBLE_BASE_URL'):
            _validate_operator_http_endpoint(
                'TTS_OPENAI_COMPATIBLE_BASE_URL', env['TTS_OPENAI_COMPATIBLE_BASE_URL'], errors
            )
    elif any(env.get(name) for name in tts_settings):
        errors.append('sherpa_onnx TTS must not retain openai_compatible endpoint or credentials')

    icon_transport = env.get('APP_ICON_GENERATION_TRANSPORT', '')
    icon_settings = (
        'IMAGE_GENERATION_OPENAI_COMPATIBLE_BASE_URL',
        'IMAGE_GENERATION_OPENAI_COMPATIBLE_API_KEY',
        'IMAGE_GENERATION_OPENAI_COMPATIBLE_MODEL',
    )
    if icon_transport not in {'local_template', 'openai_compatible'}:
        errors.append('APP_ICON_GENERATION_TRANSPORT must be local_template or openai_compatible')
    elif icon_transport == 'openai_compatible':
        missing_icon = sorted(name for name in icon_settings if not env.get(name))
        if missing_icon:
            errors.append(f'openai_compatible app-icon generation requires: {", ".join(missing_icon)}')
        elif env.get('IMAGE_GENERATION_OPENAI_COMPATIBLE_BASE_URL'):
            _validate_operator_http_endpoint(
                'IMAGE_GENERATION_OPENAI_COMPATIBLE_BASE_URL',
                env['IMAGE_GENERATION_OPENAI_COMPATIBLE_BASE_URL'],
                errors,
            )
    elif any(env.get(name) for name in icon_settings):
        errors.append('local_template app-icon generation must not retain openai_compatible endpoint or credentials')

    public_objects = urlsplit(env.get('PUBLIC_OBJECTS_URL', ''))
    public_objects_origin = f'{public_objects.scheme}://{public_objects.netloc}' if public_objects.netloc else ''
    firmware_manifest = urlsplit(env.get('FIRMWARE_RELEASE_MANIFEST_URL', ''))
    firmware_asset_origin = env.get('FIRMWARE_RELEASE_ASSET_ORIGIN', '').rstrip('/')
    firmware_manifest_parts = firmware_manifest.path.strip('/').split('/', 1)
    if (
        firmware_manifest.scheme != 'https'
        or not firmware_manifest.netloc
        or firmware_manifest.username is not None
        or firmware_manifest.password is not None
        or firmware_manifest.query
        or firmware_manifest.fragment
        or len(firmware_manifest_parts) != 2
        or not all(firmware_manifest_parts)
    ):
        errors.append('FIRMWARE_RELEASE_MANIFEST_URL must be an explicit public HTTPS object URL')
    elif f'{firmware_manifest.scheme}://{firmware_manifest.netloc}' != public_objects_origin:
        errors.append('FIRMWARE_RELEASE_MANIFEST_URL must use the exact PUBLIC_OBJECTS_URL origin')
    if firmware_asset_origin != public_objects_origin:
        errors.append('FIRMWARE_RELEASE_ASSET_ORIGIN must equal the exact PUBLIC_OBJECTS_URL origin')
    for name, minimum, maximum in (
        ('REALTIME_RELAY_MAX_MESSAGE_BYTES', 1024, 8_388_608),
        ('REALTIME_RELAY_MAX_SESSION_SECONDS', 1, 3600),
    ):
        try:
            value = int(env.get(name, ''))
        except ValueError:
            value = 0
        if value < minimum or value > maximum:
            errors.append(f'{name} must be between {minimum} and {maximum}')

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
        speaker_model_dir = Path(env.get('SPEAKER_MODEL_HOST_DIR', ''))
        if not speaker_model_dir.is_absolute() or not speaker_model_dir.is_dir():
            errors.append('SPEAKER_MODEL_HOST_DIR must be an existing absolute directory')
        elif not (speaker_model_dir / 'speaker.onnx').is_file():
            errors.append('SPEAKER_MODEL_HOST_DIR is missing required file: speaker.onnx')
        diarization_audio = Path(env.get('MLX_MOSS_DIARIZE_ACCEPTANCE_WAV_HOST_PATH', ''))
        if not diarization_audio.is_absolute() or not diarization_audio.is_file():
            errors.append('MLX_MOSS_DIARIZE_ACCEPTANCE_WAV_HOST_PATH must be an existing absolute WAV file')
        elif diarization_audio.suffix.lower() != '.wav':
            errors.append('MLX_MOSS_DIARIZE_ACCEPTANCE_WAV_HOST_PATH must name a WAV file')
        if tts_provider == 'sherpa_onnx':
            tts_model_dir = Path(env.get('TTS_MODEL_HOST_DIR', ''))
            if not tts_model_dir.is_absolute() or not tts_model_dir.is_dir():
                errors.append('TTS_MODEL_HOST_DIR must be an existing absolute directory for sherpa_onnx')
            else:
                missing_tts_files = [
                    name
                    for name in ('model.onnx', 'tokens.txt', 'espeak-ng-data')
                    if not (tts_model_dir / name).exists()
                ]
                if missing_tts_files:
                    errors.append('TTS_MODEL_HOST_DIR is missing required paths: ' + ', '.join(missing_tts_files))
    try:
        speaker_threads = int(env.get('SPEAKER_EMBEDDING_NUM_THREADS', ''))
    except ValueError:
        speaker_threads = 0
    if not 1 <= speaker_threads <= 64:
        errors.append('SPEAKER_EMBEDDING_NUM_THREADS must be between 1 and 64')
    try:
        tts_threads = int(env.get('TTS_SHERPA_NUM_THREADS', ''))
        tts_speaker_id = int(env.get('TTS_SHERPA_SPEAKER_ID', ''))
    except ValueError:
        tts_threads, tts_speaker_id = 0, -1
    if not 1 <= tts_threads <= 64:
        errors.append('TTS_SHERPA_NUM_THREADS must be between 1 and 64')
    if tts_speaker_id < 0:
        errors.append('TTS_SHERPA_SPEAKER_ID must be a non-negative integer')

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
