"""Process-local outbound HTTP authority policy.

This module is a runtime guard for the Python HTTP clients used by the
self-hosted backend.  It is intentionally narrower than a network firewall:
it prevents a request from being handed to ``httpx`` when its authority is
not an explicitly reviewed operator target, but it cannot constrain code that
opens a socket outside the shared client pools.  Production still needs an
external default-deny egress policy and the cutover evidence for that policy.

Neutral/self-hosted deployments may reach built-in private Compose authorities
and operator hosts listed in ``SELF_HOST_EGRESS_ALLOWLIST``.  Everything else
fails closed before DNS resolution or a transport request.  Managed profiles
retain their existing network policy and are not changed by this guard.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

NEUTRAL_DEPLOYMENT_PROFILES = frozenset({'neutral', 'self_hosted', 'self-hosted'})
ALLOWLIST_ENV = 'SELF_HOST_EGRESS_ALLOWLIST'

# Authorities that must never be reached by a neutral/model-neutral process.
# Keep this list host-based and suffix-aware so a vendor subdomain cannot be
# reintroduced by changing only the URL path or adding a subdomain.
OFFICIAL_HOST_SUFFIXES = frozenset(
    {
        'omi.me',
        'omiapi.com',
        'openai.com',
        'anthropic.com',
        'deepgram.com',
        'googleapis.com',
        'google.com',
        'generativelanguage.com',
        'hume.ai',
        'pinecone.io',
        'sentry.io',
        'posthog.com',
        'langchain.com',
        'langsmith.com',
        'xiaomimimo.com',
        'mosi.cn',
    }
)

# These are service authorities inside the reviewed self-host Compose graph.
# They are not an external-network permission and are allowed without making
# operators enumerate every internal URL used by the backend itself.
INTERNAL_SERVICE_HOSTS = frozenset(
    {
        'localhost',
        '127.0.0.1',
        '::1',
        'auth-server',
        'backend',
        'firestore-pg-migrate',
        'host.docker.internal',
        'minio',
        'nllb-translation',
        'parakeet',
        'postgres',
        'pusher',
        'qdrant',
        'realtime-relay-fixture',
        'redis',
        'searxng',
        'typesense',
    }
)


class EgressPolicyUnavailable(httpx.RequestError):
    """Typed fail-closed result raised before an outbound request is sent.

    It is also an ``httpx.RequestError`` so existing transport boundaries map
    the rejection to their normal provider-unavailable response instead of
    treating policy denial as an unexpected application 500.
    """

    def __init__(self, reason: str, *, host: str = '') -> None:
        self.code = 'deployment_capability_unavailable'
        self.reason = reason
        self.host = host
        self.retryable = False
        detail = f'{reason}' + (f' (host={host})' if host else '')
        super().__init__(detail)


def _is_neutral_profile() -> bool:
    return os.environ.get('OMI_DEPLOYMENT_PROFILE', '').strip().lower() in NEUTRAL_DEPLOYMENT_PROFILES


def _is_official_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip('.')
    return any(normalized == suffix or normalized.endswith(f'.{suffix}') for suffix in OFFICIAL_HOST_SUFFIXES)


def _is_internal_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip('.')
    if normalized in INTERNAL_SERVICE_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    # Loopback is explicitly internal.  Other private addresses remain
    # operator-declared rather than silently broadening the process boundary.
    return address.is_loopback


def _parse_allowlist() -> frozenset[str]:
    values = {
        value.strip().lower().rstrip('.') for value in os.environ.get(ALLOWLIST_ENV, '').split(',') if value.strip()
    }
    for value in values:
        if value.startswith('*.'):
            suffix = value[2:]
            if not suffix or _is_official_host(suffix):
                raise EgressPolicyUnavailable('invalid_egress_allowlist')
        elif value == '*' or '/' in value or ':' in value or '://' in value:
            raise EgressPolicyUnavailable('invalid_egress_allowlist')
        elif _is_official_host(value):
            raise EgressPolicyUnavailable('official_host_in_egress_allowlist', host=value)
    return frozenset(values)


def _allowlisted(host: str, allowlist: frozenset[str]) -> bool:
    normalized = host.strip().lower().rstrip('.')
    if normalized in allowlist:
        return True
    return any(value.startswith('*.') and normalized.endswith(f'.{value[2:]}') for value in allowlist)


def assert_http_endpoint_allowed(url: str) -> str:
    """Validate an outbound HTTP(S) URL and return its normalized hostname.

    The check is deliberately performed before any DNS lookup.  A caller can
    use the returned hostname for safe diagnostics without logging the full
    URL, which may contain a user-controlled path or query string.
    """

    if not _is_neutral_profile():
        return urlsplit(url).hostname or ''

    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise EgressPolicyUnavailable('invalid_egress_endpoint') from error
    try:
        has_userinfo = parsed.username or parsed.password
        host = (parsed.hostname or '').lower().rstrip('.')
    except ValueError as error:
        raise EgressPolicyUnavailable('invalid_egress_endpoint') from error
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or has_userinfo:
        raise EgressPolicyUnavailable('invalid_egress_endpoint')
    if not host:
        raise EgressPolicyUnavailable('invalid_egress_endpoint')
    if _is_official_host(host):
        raise EgressPolicyUnavailable('official_endpoint_forbidden', host=host)
    if _is_internal_host(host):
        return host

    allowlist = _parse_allowlist()
    if not allowlist:
        raise EgressPolicyUnavailable('egress_allowlist_not_configured', host=host)
    if not _allowlisted(host, allowlist):
        raise EgressPolicyUnavailable('endpoint_not_allowlisted', host=host)
    return host


async def enforce_httpx_request(request: httpx.Request) -> None:
    """httpx request hook used by every shared async client pool."""

    assert_http_endpoint_allowed(str(request.url))


def httpx_request_event_hooks() -> dict[str, list[Callable[[httpx.Request], Awaitable[None]]]]:
    """Return fresh event-hook storage for an ``httpx.AsyncClient`` factory."""

    return {'request': [enforce_httpx_request]}
