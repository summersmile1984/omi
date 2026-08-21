"""Runtime configuration contract for the optional MOSS-compatible provider.

The provider supports two explicitly selected wire protocols:

* ``mosi`` — the OpenMOSS file/task API, which requires ``MOSS_API_KEY``.
* ``mlx_audio`` — an operator-owned mlx-audio OpenAI-compatible server, which
  accepts multipart ``/v1/audio/transcriptions`` and may run without a key.

Neither protocol has a vendor endpoint default.  The authority is validated
before a client or transport is constructed, and known MOSS vendor hosts are
never accepted as an operator endpoint.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

MOSS_TRANSPORT_MOSI = 'mosi'
MOSS_TRANSPORT_MLX_AUDIO = 'mlx_audio'
MOSS_TRANSPORTS = frozenset({MOSS_TRANSPORT_MOSI, MOSS_TRANSPORT_MLX_AUDIO})
MOSS_DEFAULT_MODEL = 'moss-transcribe-diarize'

# The provider must not silently turn an operator deployment back into the
# managed MOSS cloud.  Keep this suffix-aware so future vendor subdomains are
# covered as well.
MOSS_VENDOR_HOST_SUFFIXES = frozenset({'mosi.cn'})
MOSS_INTERNAL_HOSTS = frozenset({'localhost', '127.0.0.1', '::1', 'moss', 'mlx-audio', 'host.docker.internal'})
MOSS_SSRF_HOSTS = frozenset(
    {
        'metadata.google.internal',
        'metadata.google.com',
        'instance-data.ec2.internal',
        'instance-data.ec2.internal.',
    }
)


class MossConfigurationError(RuntimeError):
    """Raised when the selected MOSS-compatible runtime is unsafe/unavailable."""


@dataclass(frozen=True)
class MossRuntimeConfig:
    """Resolved MOSS authority, wire protocol, model and optional credential."""

    api_key: str
    base_url: str
    transport: str
    model: str


def _is_vendor_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip('.')
    return any(normalized == suffix or normalized.endswith(f'.{suffix}') for suffix in MOSS_VENDOR_HOST_SUFFIXES)


def _is_internal_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip('.')
    if normalized in MOSS_INTERNAL_HOSTS or '.' not in normalized:
        return True
    try:
        address = ipaddress.ip_address(normalized)
        return address.is_loopback or address.is_private
    except ValueError:
        return False


def _is_ssrf_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip('.')
    if normalized in MOSS_SSRF_HOSTS or normalized == '169.254.169.254':
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_link_local or address.is_reserved or address.is_unspecified


def _audio_host_allowlisted(host: str) -> bool:
    normalized = host.strip().lower().rstrip('.')
    configured = os.getenv('MOSS_AUDIO_URL_ALLOWLIST', '')
    for item in configured.split(','):
        candidate = item.strip().lower().rstrip('.')
        if candidate and (normalized == candidate or normalized.endswith(f'.{candidate.lstrip("*.")}')):
            return True
    return False


def resolve_moss_transport(value: str | None = None) -> str:
    transport = (value if value is not None else os.getenv('MOSS_TRANSPORT', MOSS_TRANSPORT_MOSI)).strip().lower()
    if transport not in MOSS_TRANSPORTS:
        raise MossConfigurationError(f'MOSS_TRANSPORT must be one of {sorted(MOSS_TRANSPORTS)!r}; got {transport!r}')
    return transport


def validate_moss_base_url(value: str, *, env_name: str = 'MOSS_API_BASE') -> str:
    """Validate an explicit operator authority and return it without a slash."""

    raw = (value or '').strip()
    if not raw:
        raise MossConfigurationError(f'{env_name} is required when MOSS is enabled')
    if any(character.isspace() for character in raw):
        raise MossConfigurationError(f'{env_name} must not contain whitespace')
    try:
        parsed = urlsplit(raw)
        hostname = (parsed.hostname or '').rstrip('.').lower()
        port = parsed.port
    except ValueError as exc:
        raise MossConfigurationError(f'{env_name} has an invalid host or port') from exc
    if parsed.scheme not in {'http', 'https'} or not hostname:
        raise MossConfigurationError(f'{env_name} must use an explicit HTTP(S) authority')
    if parsed.username is not None or parsed.password is not None:
        raise MossConfigurationError(f'{env_name} must not contain userinfo')
    if parsed.query or parsed.fragment:
        raise MossConfigurationError(f'{env_name} must not contain a query or fragment')
    if port is not None and not (1 <= port <= 65535):
        raise MossConfigurationError(f'{env_name} has an invalid port')
    if _is_vendor_host(hostname):
        raise MossConfigurationError(f'{env_name} must not use the managed MOSS authority')
    if _is_ssrf_host(hostname):
        raise MossConfigurationError(f'{env_name} points at a blocked metadata or link-local authority')
    if parsed.scheme == 'http' and not _is_internal_host(hostname):
        raise MossConfigurationError(f'{env_name} must use HTTPS for a public operator authority')
    return raw.rstrip('/')


def validate_moss_audio_url(value: str) -> str:
    """Validate a caller-provided audio URL before any provider fetch/forward.

    Public HTTPS URLs are accepted for signed object URLs. Private, loopback,
    and container-local authorities require the explicit
    MOSS_AUDIO_URL_ALLOWLIST host list so a recording URL cannot become an
    SSRF primitive. Redirects are disabled by the clients as a second boundary.
    """

    raw = (value or '').strip()
    if not raw or any(character.isspace() for character in raw):
        raise MossConfigurationError('MOSS audio URL must be a non-empty HTTP(S) URL')
    try:
        parsed = urlsplit(raw)
        hostname = (parsed.hostname or '').rstrip('.').lower()
        parsed.port
    except ValueError as exc:
        raise MossConfigurationError('MOSS audio URL has an invalid host or port') from exc
    if parsed.scheme not in {'http', 'https'} or not hostname:
        raise MossConfigurationError('MOSS audio URL must use HTTP(S)')
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise MossConfigurationError('MOSS audio URL must not contain userinfo or a fragment')
    if _is_vendor_host(hostname):
        raise MossConfigurationError('MOSS audio URL must not use the managed MOSS authority')
    if _is_ssrf_host(hostname):
        raise MossConfigurationError('MOSS audio URL points at a blocked metadata or link-local authority')
    if _is_internal_host(hostname) and not _audio_host_allowlisted(hostname):
        raise MossConfigurationError(
            'private MOSS audio authorities require an explicit allowlist ' '(MOSS_AUDIO_URL_ALLOWLIST)'
        )
    if parsed.scheme == 'http' and not _is_internal_host(hostname):
        raise MossConfigurationError('public MOSS audio URLs must use HTTPS')
    return raw


def resolve_moss_timeout(explicit_timeout: float | None = None) -> float:
    raw = explicit_timeout if explicit_timeout is not None else os.getenv('MOSS_TIMEOUT_SECONDS', '120')
    try:
        timeout = float(raw)
    except (TypeError, ValueError) as exc:
        raise MossConfigurationError('MOSS_TIMEOUT_SECONDS must be a number') from exc
    if timeout <= 0:
        raise MossConfigurationError('MOSS_TIMEOUT_SECONDS must be greater than zero')
    return timeout


def resolve_moss_api_key(transport: str, explicit_api_key: str | None = None) -> str:
    value = explicit_api_key if explicit_api_key is not None else os.getenv('MOSS_API_KEY', '')
    normalized = value.strip()
    if transport == MOSS_TRANSPORT_MOSI and not normalized:
        raise MossConfigurationError('MOSS_API_KEY is required for the mosi transport')
    return normalized


def resolve_moss_model(transport: str, explicit_model: str | None = None) -> str:
    value = explicit_model if explicit_model is not None else os.getenv('MOSS_MODEL', '')
    model = value.strip()
    if not model and transport == MOSS_TRANSPORT_MOSI:
        return MOSS_DEFAULT_MODEL
    if not model:
        raise MossConfigurationError('MOSS_MODEL is required for the mlx_audio transport')
    return model


def resolve_moss_config(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    transport: str | None = None,
    model: str | None = None,
) -> MossRuntimeConfig:
    selected_transport = resolve_moss_transport(transport)
    authority = base_url if base_url is not None else os.getenv('MOSS_API_BASE', '')
    # Validate the authority before credentials so a missing endpoint never
    # gets masked by a missing key. This keeps the deployment boundary
    # deterministic and makes explicit operator configuration fail closed.
    validated_authority = validate_moss_base_url(authority)
    return MossRuntimeConfig(
        api_key=resolve_moss_api_key(selected_transport, api_key),
        base_url=validated_authority,
        transport=selected_transport,
        model=resolve_moss_model(selected_transport, model),
    )


def moss_is_configured() -> bool:
    try:
        resolve_moss_config()
    except MossConfigurationError:
        return False
    return True
