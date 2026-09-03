"""Patch definitions, one module per seam.

Each module exposes `patches()` returning a list of `registry.Patch`. Adding a
seam means adding a module here and listing it in `ALL`; it never means editing
an upstream file.
"""

from __future__ import annotations

from typing import List

from ..registry import Patch
from . import queue as _queue
from . import speaker_embedding as _speaker_embedding
from . import storage as _storage

ALL = (_storage, _queue, _speaker_embedding)


def collect() -> List[Patch]:
    found: List[Patch] = []
    for module in ALL:
        found.extend(module.patches())
    return found
