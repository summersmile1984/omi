"""Pure push-provider selection for managed and operator-owned deployments.

Push is optional for a self-hosted profile.  In particular, an omitted
``PUSH_PROVIDER`` must not turn an ambient Firebase credential into an
unexpected vendor egress path.  Keep this policy in ``config/`` so the
startup Firebase decision and notification helpers share one boundary.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

SUPPORTED_PUSH_PROVIDERS = frozenset({'firebase', 'disabled'})
NEUTRAL_DEPLOYMENT_PROFILES = frozenset({'neutral', 'self_hosted', 'self-hosted'})


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
        raise ValueError(f'unsupported PUSH_PROVIDER={provider!r}')
    return provider
