"""Public share-link origin selected by the deployment profile.

Managed deployments retain the historical ``https://h.omi.me`` default. A
neutral/self-hosted deployment must provide ``OMI_SHARE_BASE_URL`` explicitly;
there is deliberately no Omi-host fallback on that path. This boundary is
backend-owned because share URLs can be minted by authenticated API routes and
share hosts are also accepted while parsing natural-language search input.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

DEFAULT_SHARE_BASE_URL = 'https://h.omi.me'
_ENV_KEY = 'OMI_SHARE_BASE_URL'
NEUTRAL_DEPLOYMENT_PROFILES = frozenset({'neutral', 'self_hosted', 'self-hosted'})


class ShareOriginUnavailableError(RuntimeError):
    """Raised when a deployment cannot safely mint or parse share links."""


def _is_neutral_deployment() -> bool:
    return (os.getenv('OMI_DEPLOYMENT_PROFILE') or '').strip().lower() in NEUTRAL_DEPLOYMENT_PROFILES


def _is_omi_operated_host(host: str) -> bool:
    normalized = host.lower().rstrip('.')
    return (
        normalized == 'omi.me'
        or normalized.endswith('.omi.me')
        or normalized == 'omiapi.com'
        or normalized.endswith('.omiapi.com')
    )


def _canonical_self_hosted_origin(raw: str) -> str:
    value = raw.strip()
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ShareOriginUnavailableError('OMI_SHARE_BASE_URL is not a valid URL') from error
    host = (parsed.hostname or '').lower().rstrip('.')
    if (
        parsed.scheme.lower() != 'https'
        or not parsed.netloc
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {'', '/'}
        or _is_omi_operated_host(host)
    ):
        raise ShareOriginUnavailableError('self-hosted OMI_SHARE_BASE_URL must be an explicit non-Omi HTTPS origin')
    try:
        port = parsed.port
    except ValueError as error:
        raise ShareOriginUnavailableError('OMI_SHARE_BASE_URL contains an invalid port') from error
    if port == 443:
        port = None
    return f'https://{host}' + (f':{port}' if port is not None else '')


def share_base_url() -> str:
    """Return the canonical share origin (no trailing slash).

    The managed default is intentionally retained for compatibility. For a
    neutral/self-hosted profile, a missing value is an unavailable capability,
    not permission to emit a link to Omi's public host.
    """
    configured = os.getenv(_ENV_KEY)
    raw = (configured or '').strip()
    if _is_neutral_deployment():
        if not raw:
            raise ShareOriginUnavailableError('self-hosted share links require an explicit OMI_SHARE_BASE_URL')
        return _canonical_self_hosted_origin(raw)
    if not raw:
        raw = DEFAULT_SHARE_BASE_URL
    if '://' not in raw:
        raw = f'https://{raw}'
    return raw.rstrip('/')


def share_host() -> str:
    """Hostname used when minting share URLs (lowercase netloc without userinfo)."""
    parsed = urlsplit(share_base_url())
    host = (parsed.hostname or '').lower().rstrip('.')
    if not host:
        raise ShareOriginUnavailableError('OMI_SHARE_BASE_URL does not contain a hostname')
    return host


def accepted_share_hosts() -> frozenset[str]:
    """Hosts accepted when parsing inbound share URLs.

    Managed deployments accept the historical ``h.omi.me`` host in addition to
    a configured override. Neutral/self-hosted deployments accept only their
    explicit operator origin, so an Omi URL cannot be mistaken for a local
    share authority.
    """
    try:
        host = share_host()
    except ShareOriginUnavailableError:
        return frozenset()
    hosts = {host}
    if not _is_neutral_deployment():
        hosts.add('h.omi.me')
    return frozenset(hosts)


def build_share_url(path: str) -> str:
    """Join ``share_base_url()`` with a path that starts with ``/``."""
    if not path.startswith('/'):
        path = f'/{path}'
    return f'{share_base_url()}{path}'
