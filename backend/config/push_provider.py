"""Pure push-provider selection for managed and operator-owned deployments.

Push is optional for a self-hosted profile.  In particular, an omitted
``PUSH_PROVIDER`` must not turn an ambient Firebase credential into an
unexpected vendor egress path.  Keep this policy in ``config/`` so the
startup Firebase decision and notification helpers share one boundary.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# ``webhook`` is intentionally not a supported provider yet.  A generic
# notification webhook cannot safely be treated as a drop-in replacement for
# FCM: it needs an operator-owned device identity contract, authenticated
# delivery receipts, and a reviewed retry/idempotency policy.  Keep the name
# reserved so a typo cannot silently turn into an unsigned HTTP client.
SUPPORTED_PUSH_PROVIDERS = frozenset({'firebase', 'disabled'})
RESERVED_UNIMPLEMENTED_PUSH_PROVIDERS = frozenset({'webhook'})
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
        if provider in RESERVED_UNIMPLEMENTED_PUSH_PROVIDERS:
            raise ValueError(
                "unsupported PUSH_PROVIDER='webhook': operator-owned webhook delivery is reserved but not implemented; "
                "use PUSH_PROVIDER=disabled"
            )
        raise ValueError(f'unsupported PUSH_PROVIDER={provider!r}')
    return provider
