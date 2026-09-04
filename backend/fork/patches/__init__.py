"""Patch definitions, one module per seam.

Each module exposes `patches()` returning a list of `registry.Patch`. Adding a
seam means adding a module here and listing it in `ALL`; it never means editing
an upstream file.
"""

from __future__ import annotations

from typing import List

from ..registry import Patch
from . import account_deletion as _account_deletion
from . import auth as _auth
from . import embedding as _embedding
from . import capabilities as _capabilities
from . import speech as _speech
from . import provider_guard as _provider_guard
from . import queue as _queue
from . import vector as _vector
from . import speaker_embedding as _speaker_embedding
from . import storage as _storage

ALL = (
    _auth,
    _embedding,
    _capabilities,
    _speech,
    _account_deletion,
    _provider_guard,
    _storage,
    _queue,
    _speaker_embedding,
    _vector,
)


def collect() -> List[Patch]:
    found: List[Patch] = []
    for module in ALL:
        found.extend(module.patches())
    return found
