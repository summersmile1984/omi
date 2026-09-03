"""ASGI entry point for fork deployments.

    uvicorn fork.main:app

Imports upstream's app unchanged, then applies the profile's patches. Upstream's
`main.py` is never edited; in `omi_cloud` mode this module is a no-op wrapper,
so the same image can serve upstream behavior.
"""

from __future__ import annotations

import logging

from . import profile
from .patches import collect
from .registry import build_registry

logger = logging.getLogger(__name__)


def bootstrap():
    """Resolve the profile and apply its patches. Raises on any failure."""
    row = profile.current()
    registry = build_registry(collect()).apply(row)
    logger.info("fork profile %s (%s); %s", row["name"], row["identity_provider"], registry.summary())
    return registry


bootstrap()

# Imported after patching so upstream module-level code sees the fork's
# adapters, not the cloud clients it would otherwise construct.
from main import app  # noqa: E402,F401
