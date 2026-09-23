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
from .. import firmware as _firmware
from . import llm as _llm
from . import capabilities as _capabilities
from . import consolidation as _consolidation
from . import canonical_memory as _canonical_memory
from . import conversation_search as _conversation_search
from . import memory_clock as _memory_clock
from . import speech as _speech
from . import provider_guard as _provider_guard
from . import queue as _queue
from . import vector as _vector
from . import speaker_embedding as _speaker_embedding
from . import storage as _storage

ALL = (
    _auth,
    _embedding,
    _firmware,
    _llm,
    _capabilities,
    _consolidation,
    _canonical_memory,
    _conversation_search,
    _memory_clock,
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


def collect_memory_maintenance() -> List[Patch]:
    """Admit the existing consolidation and projection owners without ASGI.

    Pending user submissions require canonical processing before chat may read
    them. The worker uses the same selected model and apply fences as the API;
    it does not serve HTTP, consume Redis jobs, or touch object storage.
    """
    provider_names = {
        'provider.receipt-fence.database.vector_db',
        'provider.receipt-fence.utils.memory.atom_keyword_index',
    }
    return [
        *_memory_clock.patches(),
        *_canonical_memory.patches(),
        *_consolidation.patches(),
        *(patch for patch in _llm.patches() if patch.module != 'utils.retrieval.agentic'),
        *_embedding.patches(),
        *_vector.patches(),
        *(patch for patch in _provider_guard.patches() if patch.name in provider_names),
    ]
