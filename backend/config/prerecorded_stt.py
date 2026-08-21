"""Pure runtime contract for pre-recorded speech-to-text providers.

Keep model-token routing and provider configuration requirements here so runtime
selection and deployment validation cannot drift apart.  This module deliberately
does not construct clients, read files, or snapshot environment values at import.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from config.stt_provider_policy import (
    DEEPGRAM_SELF_HOSTED_PROVIDER,
    MIMO_PROVIDER,
    MLX_MOSS_DIARIZE_PROVIDER,
    MODULATE_PROVIDER,
    MOSS_PROVIDER,
    PARAKEET_PROVIDER,
    SENSEVOICE_PROVIDER,
    STTServingSurface,
    default_models_for_surface,
    provider_for_model_token as policy_provider_for_model_token,
    provider_is_enabled,
)

STT_PRERECORDED_MODEL_ENV = 'STT_PRERECORDED_MODEL'
# Compatibility export for callers. Its value is owned by stt_provider_policy.
DEFAULT_STT_PRERECORDED_MODELS = default_models_for_surface(STTServingSurface.PRERECORDED)


class TranscriptionOutcome(str, Enum):
    """Closed, low-cardinality vocabulary for every accepted transcription."""

    SUCCESS = 'success'
    EXPECTED_SILENCE = 'expected_silence'
    EMPTY_UNEXPECTED = 'empty_unexpected'
    TIMEOUT = 'timeout'
    UPSTREAM_ERROR = 'upstream_error'
    CONFIG_ERROR = 'config_error'
    INVALID_INPUT = 'invalid_input'


class PrerecordedSTTService:
    DEEPGRAM = 'deepgram'
    MODULATE = 'modulate'
    PARAKEET = 'parakeet'
    SENSEVOICE = 'sensevoice'
    MIMO = 'mimo'
    MOSS = 'moss'
    MLX_MOSS_DIARIZE = 'mlx_moss_diarize'


class PrerecordedSTTConfigurationError(RuntimeError):
    """A selected pre-recorded STT provider is not configured on this runtime."""

    def __init__(self, provider: str, missing_env: str):
        self.provider = provider
        self.missing_env = missing_env
        super().__init__(f'{provider} pre-recorded STT requires {missing_env}')


@dataclass(frozen=True)
class ProviderEnvironmentContract:
    """Environment required before invoking one provider.

    ``required_when_model_source_is_opaque`` covers deployment manifests where the
    selected model is secret-backed and therefore unavailable to a static checker.
    Every dependency an opaque selection can activate opts in; request-scoped BYOK
    remains a runtime bypass, not a substitute for background-process credentials.
    """

    required_env: tuple[str, ...] = ()
    required_when_model_source_is_opaque: bool = False


@dataclass(frozen=True)
class MlxMossDiarizeConfig:
    """Validated operator-owned mlx-audio transcription authority."""

    endpoint: str
    model: str
    api_key: str | None


PROVIDER_ENVIRONMENT_CONTRACTS: Mapping[str, ProviderEnvironmentContract] = {
    PrerecordedSTTService.DEEPGRAM: ProviderEnvironmentContract(
        required_env=('DEEPGRAM_API_KEY',),
        required_when_model_source_is_opaque=True,
    ),
    PrerecordedSTTService.MODULATE: ProviderEnvironmentContract(
        required_env=('MODULATE_API_KEY',),
        required_when_model_source_is_opaque=True,
    ),
    # The Parakeet model token and its separately deployed endpoint must move as one
    # contract.  Validate the endpoint even when the token itself is secret-backed.
    PrerecordedSTTService.PARAKEET: ProviderEnvironmentContract(
        required_env=('HOSTED_PARAKEET_API_URL',),
        required_when_model_source_is_opaque=True,
    ),
    PrerecordedSTTService.SENSEVOICE: ProviderEnvironmentContract(required_env=('SENSEVOICE_MODEL_DIR',)),
    PrerecordedSTTService.MIMO: ProviderEnvironmentContract(
        required_env=('MIMO_API_KEY', 'MIMO_API_BASE'),
    ),
    # MOSS is selected only by an explicit literal token in the cloud-neutral
    # runtime. Do not make an opaque upstream deployment require its credential.
    PrerecordedSTTService.MOSS: ProviderEnvironmentContract(required_env=('MOSS_API_KEY', 'MOSS_API_BASE')),
    PrerecordedSTTService.MLX_MOSS_DIARIZE: ProviderEnvironmentContract(
        required_env=('MLX_MOSS_DIARIZE_ENDPOINT', 'MLX_MOSS_DIARIZE_MODEL'),
    ),
}


_MLX_MOSS_TRANSCRIPTIONS_PATH = '/v1/audio/transcriptions'
_PRIVATE_HTTP_HOST_SUFFIXES = ('.internal', '.local', '.svc', '.svc.cluster.local')
_CGNAT_NETWORK = ipaddress.ip_network('100.64.0.0/10')
_RFC1918_NETWORKS = (
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
)
_ULA_NETWORK = ipaddress.ip_network('fc00::/7')
_UNSAFE_METADATA_HOSTNAMES = frozenset(
    {
        'instance-data',
        'instance-data.ec2.internal',
        'metadata',
        'metadata.aws.internal',
        'metadata.azure.internal',
        'metadata.google.internal',
    }
)
_LEGACY_IPV4_LITERAL = re.compile(r'(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+)){0,3}')


def is_unsafe_network_hostname(hostname: str) -> bool:
    """Reject network authorities that must never receive credentials or audio."""

    normalized = hostname.rstrip('.').lower()
    if normalized in _UNSAFE_METADATA_HOSTNAMES or any(
        normalized.endswith(f'.{metadata_hostname}') for metadata_hostname in _UNSAFE_METADATA_HOSTNAMES
    ):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        # URL clients and operating-system resolvers can accept legacy integer,
        # octal, and hexadecimal IPv4 spellings that ``ipaddress`` intentionally
        # rejects. Treat numeric-looking authorities as unsafe instead of letting
        # them become operator-controlled single-label DNS names.
        return _LEGACY_IPV4_LITERAL.fullmatch(normalized) is not None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return False
    if isinstance(address, ipaddress.IPv4Address) and any(address in network for network in _RFC1918_NETWORKS):
        return False
    if isinstance(address, ipaddress.IPv6Address) and address in _ULA_NETWORK:
        return False
    return (
        address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or (isinstance(address, ipaddress.IPv4Address) and address in _CGNAT_NETWORK)
        or not address.is_global
    )


def is_private_operator_hostname(hostname: str) -> bool:
    normalized = hostname.rstrip('.').lower()
    if is_unsafe_network_hostname(normalized):
        return False
    if normalized in {'localhost', 'host.docker.internal'}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        # Single-label container/service names and reserved internal DNS suffixes
        # are operator-controlled private network authorities. Do not resolve DNS
        # in this pure configuration module.
        return '.' not in normalized or normalized.endswith(_PRIVATE_HTTP_HOST_SUFFIXES)
    if isinstance(address, ipaddress.IPv4Address):
        return address.is_loopback or any(address in network for network in _RFC1918_NETWORKS)
    return address.is_loopback or address in _ULA_NETWORK


def get_mlx_moss_diarize_config(env: Mapping[str, str] | None = None) -> MlxMossDiarizeConfig:
    """Read and validate the explicit mlx-audio endpoint/model at call time."""

    source = os.environ if env is None else env
    endpoint = (source.get('MLX_MOSS_DIARIZE_ENDPOINT') or '').strip()
    model = (source.get('MLX_MOSS_DIARIZE_MODEL') or '').strip()
    api_key = (source.get('MLX_MOSS_DIARIZE_API_KEY') or '').strip() or None
    if not endpoint:
        raise PrerecordedSTTConfigurationError(
            PrerecordedSTTService.MLX_MOSS_DIARIZE,
            'MLX_MOSS_DIARIZE_ENDPOINT',
        )
    if not model:
        raise PrerecordedSTTConfigurationError(
            PrerecordedSTTService.MLX_MOSS_DIARIZE,
            'MLX_MOSS_DIARIZE_MODEL',
        )

    parsed = urlsplit(endpoint)
    hostname = (parsed.hostname or '').rstrip('.').lower()
    invalid_url = (
        parsed.scheme not in {'http', 'https'}
        or not hostname
        or parsed.path != _MLX_MOSS_TRANSCRIPTIONS_PATH
        or bool(parsed.query)
        or bool(parsed.fragment)
        or parsed.username is not None
        or parsed.password is not None
    )
    if invalid_url:
        raise PrerecordedSTTConfigurationError(
            PrerecordedSTTService.MLX_MOSS_DIARIZE,
            'MLX_MOSS_DIARIZE_ENDPOINT',
        )
    if (
        is_unsafe_network_hostname(hostname)
        or hostname == 'mosi.cn'
        or hostname.endswith('.mosi.cn')
        or hostname == 'omi.me'
        or hostname.endswith('.omi.me')
    ):
        raise PrerecordedSTTConfigurationError(
            PrerecordedSTTService.MLX_MOSS_DIARIZE,
            'MLX_MOSS_DIARIZE_ENDPOINT',
        )
    if parsed.scheme == 'http' and not is_private_operator_hostname(hostname):
        raise PrerecordedSTTConfigurationError(
            PrerecordedSTTService.MLX_MOSS_DIARIZE,
            'MLX_MOSS_DIARIZE_ENDPOINT',
        )
    if parsed.scheme == 'https' and not is_private_operator_hostname(hostname) and api_key is None:
        raise PrerecordedSTTConfigurationError(
            PrerecordedSTTService.MLX_MOSS_DIARIZE,
            'MLX_MOSS_DIARIZE_API_KEY',
        )
    return MlxMossDiarizeConfig(endpoint=endpoint, model=model, api_key=api_key)


def parse_prerecorded_models(raw: str | None) -> tuple[str, ...]:
    """Parse the configured model preference, defaulting to non-Deepgram providers."""
    if raw is None:
        return DEFAULT_STT_PRERECORDED_MODELS
    models = tuple(model.strip() for model in raw.split(',') if model.strip())
    return models or DEFAULT_STT_PRERECORDED_MODELS


def get_prerecorded_models(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Read the current model preference instead of freezing it during import."""
    source = os.environ if env is None else env
    return parse_prerecorded_models(source.get(STT_PRERECORDED_MODEL_ENV))


def provider_for_model_token(model: str) -> str | None:
    provider = policy_provider_for_model_token(model)
    if provider == MODULATE_PROVIDER:
        return PrerecordedSTTService.MODULATE
    if provider == PARAKEET_PROVIDER:
        return PrerecordedSTTService.PARAKEET
    if provider == SENSEVOICE_PROVIDER:
        return PrerecordedSTTService.SENSEVOICE
    if provider == MIMO_PROVIDER:
        return PrerecordedSTTService.MIMO
    if provider == MOSS_PROVIDER:
        return PrerecordedSTTService.MOSS
    if provider == MLX_MOSS_DIARIZE_PROVIDER:
        return PrerecordedSTTService.MLX_MOSS_DIARIZE
    if provider == DEEPGRAM_SELF_HOSTED_PROVIDER:
        return PrerecordedSTTService.DEEPGRAM
    return None


def providers_for_model_config(raw: str) -> tuple[str, ...]:
    """Return every non-retired provider a literal config can activate, including fallback."""
    providers: list[str] = []
    for model in parse_prerecorded_models(raw):
        provider = provider_for_model_token(model)
        if (
            provider is not None
            and provider_is_enabled(provider, STTServingSurface.PRERECORDED)
            and provider not in providers
        ):
            providers.append(provider)
    # Explicit batch-only adapters own their route and never silently fall back
    # to a managed provider omitted from the deployment's model list.
    if any(
        provider in providers
        for provider in (
            PrerecordedSTTService.MOSS,
            PrerecordedSTTService.MLX_MOSS_DIARIZE,
            PrerecordedSTTService.SENSEVOICE,
            PrerecordedSTTService.MIMO,
        )
    ):
        return tuple(providers)
    # Retired/unknown tokens and unsupported languages fall through to the
    # non-Deepgram defaults. Include both because language capability decides
    # which one serves the request.
    for model in DEFAULT_STT_PRERECORDED_MODELS:
        provider = provider_for_model_token(model)
        if (
            provider is not None
            and provider_is_enabled(provider, STTServingSurface.PRERECORDED)
            and provider not in providers
        ):
            providers.append(provider)
    return tuple(providers)


def required_env_for_provider(provider: str) -> tuple[str, ...]:
    contract = PROVIDER_ENVIRONMENT_CONTRACTS.get(provider)
    return contract.required_env if contract is not None else ()


def required_env_for_model_config(raw: str | None, *, source_is_opaque: bool = False) -> tuple[str, ...]:
    """Return deployment requirements for a literal or opaque model selection."""
    if source_is_opaque:
        providers = tuple(
            provider
            for provider, contract in PROVIDER_ENVIRONMENT_CONTRACTS.items()
            if contract.required_when_model_source_is_opaque
            and provider_is_enabled(provider, STTServingSurface.PRERECORDED)
        )
    else:
        providers = providers_for_model_config(raw or '')

    required: list[str] = []
    for provider in providers:
        for env_name in required_env_for_provider(provider):
            if env_name not in required:
                required.append(env_name)
    return tuple(required)


def missing_provider_environment(provider: str, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    source = os.environ if env is None else env
    return tuple(name for name in required_env_for_provider(provider) if not (source.get(name) or '').strip())


def require_provider_environment(provider: str, env: Mapping[str, str] | None = None) -> None:
    missing = missing_provider_environment(provider, env)
    if missing:
        raise PrerecordedSTTConfigurationError(provider, missing[0])
