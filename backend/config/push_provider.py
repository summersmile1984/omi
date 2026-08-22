"""Pure push-provider selection for managed and operator-owned deployments.

Push is optional for a self-hosted profile.  In particular, an omitted
``PUSH_PROVIDER`` must not turn an ambient Firebase credential into an
unexpected vendor egress path.  Keep this policy in ``config/`` so the
startup Firebase decision and notification helpers share one boundary.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
import re

# ``webhook`` is an explicit operator-owned bridge.  Its endpoint, secret-file
# and receipt contract are validated by ``utils.push_webhook`` before the first
# request; it never falls through to Firebase.
SUPPORTED_PUSH_PROVIDERS = frozenset({'firebase', 'disabled', 'webhook'})
NEUTRAL_DEPLOYMENT_PROFILES = frozenset({'neutral', 'self_hosted', 'self-hosted'})

# Device registration is intentionally provider-neutral.  ``fcm_token`` is a
# released wire-field name, but its value is an opaque registration token and
# must not be interpreted as an FCM credential by an operator bridge.
PUSH_DEVICE_TOKEN_SCHEMA = 'omi.push.device-token.v1'
PUSH_DEVICE_TOKEN_TYPE = 'opaque_registered_token'
_DEVICE_PLATFORM = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
_DEVICE_ID_HASH = re.compile(r'^[0-9a-f]{64}$')


def normalize_device_registration(
    *, token: str, platform: str | None = None, device_id_hash: str | None = None
) -> dict[str, str]:
    """Validate the provider-neutral mobile token registration contract.

    The backend stores an opaque token and stable device key only.  Firebase,
    APNs, or an operator webhook owns interpretation of the token after this
    boundary.  Missing legacy headers remain representable as ``unknown`` and
    ``default`` so released clients can register without a migration dance.
    """

    normalized_token = token.strip()
    if not normalized_token or len(normalized_token.encode('utf-8')) > 4096:
        raise ValueError('push device token must be non-empty and at most 4096 UTF-8 bytes')
    normalized_platform = (platform or 'unknown').strip().lower() or 'unknown'
    if normalized_platform != 'unknown' and not _DEVICE_PLATFORM.fullmatch(normalized_platform):
        raise ValueError('push device platform must be a lowercase identifier')
    normalized_device_id = (device_id_hash or 'default').strip().lower() or 'default'
    if normalized_device_id != 'default' and not _DEVICE_ID_HASH.fullmatch(normalized_device_id):
        raise ValueError('X-Device-Id-Hash must be a lowercase SHA-256 hex digest')
    return {
        'schema': PUSH_DEVICE_TOKEN_SCHEMA,
        'token_type': PUSH_DEVICE_TOKEN_TYPE,
        'platform': normalized_platform,
        'device_id_hash': normalized_device_id,
        'token': normalized_token,
    }


def selected_push_provider(env: Mapping[str, str] | None = None) -> str:
    """Resolve the deployment-selected push provider without side effects.

    Managed deployments preserve the historical Firebase default.  A neutral
    profile defaults to ``disabled`` until an operator explicitly selects a
    provider, so a leaked Firebase credential cannot enable vendor delivery.
    """

    values = os.environ if env is None else env
    configured = (values.get('PUSH_PROVIDER') or '').strip().lower()
    profile = (values.get('OMI_DEPLOYMENT_PROFILE') or '').strip().lower()
    if profile in NEUTRAL_DEPLOYMENT_PROFILES:
        # Keep this selector safe when low-level notification helpers are
        # imported without the normal application startup validation. The
        # explicit value is still rejected by validate_push_provider so an
        # invalid production config fails loudly instead of being accepted.
        if configured == 'firebase':
            return 'disabled'
        return configured or 'disabled'
    return configured or 'firebase'


def validate_push_provider(env: Mapping[str, str] | None = None) -> str:
    """Resolve and validate the push provider before any SDK is initialized."""

    values = os.environ if env is None else env
    profile = (values.get('OMI_DEPLOYMENT_PROFILE') or '').strip().lower()
    configured = (values.get('PUSH_PROVIDER') or '').strip().lower()
    if profile in NEUTRAL_DEPLOYMENT_PROFILES and configured == 'firebase':
        raise ValueError('PUSH_PROVIDER=firebase is forbidden in neutral/self-hosted deployments')

    provider = selected_push_provider(values)
    if provider not in SUPPORTED_PUSH_PROVIDERS:
        raise ValueError(f'unsupported PUSH_PROVIDER={provider!r}')
    if provider == 'webhook':
        # Import lazily so the neutral/managed Firebase startup path stays
        # independent from the optional HTTP bridge.  This also ensures a
        # malformed operator configuration fails before the server starts.
        from utils.push_webhook import resolve_push_webhook_config

        resolve_push_webhook_config(env)
    return provider
