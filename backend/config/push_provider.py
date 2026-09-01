"""Pure push-provider selection for managed and operator-owned deployments.

Push is optional for a self-hosted profile.  In particular, an omitted
``PUSH_PROVIDER`` must not turn an ambient Firebase credential into an
unexpected vendor egress path.  Keep this policy in ``config/`` so the
startup Firebase decision and notification helpers share one boundary.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

SUPPORTED_PUSH_PROVIDERS = frozenset({'firebase', 'disabled', 'webhook'})
NEUTRAL_DEPLOYMENT_PROFILES = frozenset({'neutral', 'self_hosted', 'self-hosted'})


class PushProviderConfigurationError(ValueError):
    """Raised when an explicitly selected push provider is not configured safely."""


@dataclass(frozen=True)
class PushWebhookConfig:
    """Credential-bearing webhook settings kept out of runtime evidence."""

    url: str
    secret: str
    timeout_seconds: float


def push_webhook_config(env: Mapping[str, str] | None = None) -> PushWebhookConfig:
    """Load the operator-owned webhook contract without exposing its secret.

    The provider only admits public HTTPS endpoints.  Delivery performs a
    second DNS/IP safety check and pins the connection to the resolved address;
    keeping this loader format-only lets startup remain deterministic while
    preserving the runtime SSRF boundary.
    """

    values = os.environ if env is None else env
    url = (values.get('PUSH_WEBHOOK_URL') or '').strip()
    secret = values.get('PUSH_WEBHOOK_SECRET') or ''
    if not url:
        raise PushProviderConfigurationError('PUSH_WEBHOOK_URL is required for PUSH_PROVIDER=webhook')
    if not secret or len(secret) < 16:
        raise PushProviderConfigurationError('PUSH_WEBHOOK_SECRET must contain at least 16 characters')

    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise PushProviderConfigurationError('PUSH_WEBHOOK_URL must be a valid public HTTPS URL') from error
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in url)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise PushProviderConfigurationError(
            'PUSH_WEBHOOK_URL must be a credential-free HTTPS URL without query or fragment'
        )

    raw_timeout = (values.get('PUSH_WEBHOOK_TIMEOUT_SECONDS') or '5').strip()
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError as error:
        raise PushProviderConfigurationError('PUSH_WEBHOOK_TIMEOUT_SECONDS must be between 1 and 10 seconds') from error
    if not 1 <= timeout_seconds <= 10:
        raise PushProviderConfigurationError('PUSH_WEBHOOK_TIMEOUT_SECONDS must be between 1 and 10 seconds')

    return PushWebhookConfig(url=url, secret=secret, timeout_seconds=timeout_seconds)


def selected_push_provider(env: Mapping[str, str] | None = None) -> str:
    """Resolve the deployment-selected push provider without side effects.

    Managed deployments preserve the historical Firebase default.  A neutral
    profile defaults to ``disabled`` until an operator explicitly selects a
    provider, so a leaked Firebase credential cannot enable vendor delivery.
    """

    values = os.environ if env is None else env
    configured = (values.get('PUSH_PROVIDER') or '').strip().lower()
    if configured:
        return configured
    profile = (values.get('OMI_DEPLOYMENT_PROFILE') or '').strip().lower()
    return 'disabled' if profile in NEUTRAL_DEPLOYMENT_PROFILES else 'firebase'


def validate_push_provider(env: Mapping[str, str] | None = None) -> str:
    """Resolve and validate the push provider before any SDK is initialized."""

    provider = selected_push_provider(env)
    if provider not in SUPPORTED_PUSH_PROVIDERS:
        raise PushProviderConfigurationError(f'unsupported PUSH_PROVIDER={provider!r}')
    if provider == 'webhook':
        push_webhook_config(env)
    return provider
