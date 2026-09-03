"""Resolve which deployment profile this backend process is running as.

The table is generated from `deploy/profiles/` by `scripts/profiles/render.py`
(step S1); this module only selects a row and validates the selection. Keeping
selection here rather than in each patch means a patch asks "is the store
firestore_pg?" instead of re-deriving it from environment variables, and the
answer is the same one the clients were built against.

Selection order:
  1. OMI_DEPLOYMENT_PROFILE, e.g. "self_hosted.production"
  2. OMI_DEPLOYMENT_TARGET + OMI_ENV_STAGE
  3. omi_cloud.production -- upstream behavior, no patches
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

GENERATED_TABLE = Path(__file__).with_name("deployment_profiles.generated.json")

DEFAULT_PROFILE = "omi_cloud.production"

# Upstream's stage names do not all map onto the fork's stage axis; only the
# ones that do are translated, so a mismatch is visible rather than guessed.
STAGE_ALIASES = {"prod": "production", "dev": "beta", "local": "local", "offline": "local"}


class ProfileError(RuntimeError):
    pass


def _load_table() -> dict:
    if not GENERATED_TABLE.exists():
        raise ProfileError(
            f"{GENERATED_TABLE.name} is missing. Generate it with: "
            f"scripts/profiles/render.py --target <target> --brand <brand>"
        )
    return json.loads(GENERATED_TABLE.read_text(encoding="utf-8"))


def _requested_name() -> str:
    explicit = os.getenv("OMI_DEPLOYMENT_PROFILE", "").strip()
    if explicit:
        return explicit
    target = os.getenv("OMI_DEPLOYMENT_TARGET", "").strip()
    if not target:
        return DEFAULT_PROFILE
    raw_stage = os.getenv("OMI_ENV_STAGE", "prod").strip()
    stage = STAGE_ALIASES.get(raw_stage, raw_stage)
    return f"{target}.{stage}"


@lru_cache(maxsize=1)
def current() -> dict:
    """The resolved profile row for this process.

    Cached: profile selection is a boot-time decision, and a process that
    changed identity provider or data plane mid-flight would be a bug, not a
    feature. Tests call `reset()`.
    """
    table = _load_table()
    profiles = table.get("profiles", {})
    name = _requested_name()

    if name not in profiles:
        rendered_for = table.get("target", "?")
        raise ProfileError(
            f"deployment profile '{name}' is not in the generated table "
            f"(rendered for target '{rendered_for}': {', '.join(sorted(profiles))}). "
            f"Either OMI_DEPLOYMENT_PROFILE is wrong, or the image was built with "
            f"the wrong --target."
        )

    row = profiles[name]
    identity = row.get("identity_provider")
    # The same fail-closed pairing the renderer enforces, re-checked here because
    # the table is a build artifact and the image could have been mismatched.
    if row.get("target") == "omi_cloud" and identity != "firebase":
        raise ProfileError(f"{name}: omi_cloud requires firebase identity, table says {identity!r}")
    if row.get("target") != "omi_cloud" and identity != "better_auth":
        raise ProfileError(f"{name}: {row.get('target')} requires better_auth identity, table says {identity!r}")
    return row


def reset() -> None:
    """Drop the cached selection. Tests only."""
    current.cache_clear()


def is_upstream_mode() -> bool:
    """True when this process must behave exactly as upstream does."""
    return current().get("target") == "omi_cloud"


def capability(name: str, default=None):
    return current().get("capabilities", {}).get(name, default)


def data_plane(component: str) -> str:
    return current().get("data_plane", {}).get(component, "")
