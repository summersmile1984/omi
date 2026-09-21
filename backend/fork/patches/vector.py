"""Bind upstream's index handle to the selected vector authority before routers.

For local dev / linux bring-up, we deliberately skip this patch: the only
production-ready vector authority is Qdrant, and the local dev harness has
neither a Qdrant container nor a pgvector / Ollama backend wired. Skipping
leaves ``database.vector_db.index = None`` (the upstream fallback when no
Pinecone credentials are present), so vector-backed business operations
fail closed at request time rather than at boot. The backend still serves
``/health`` and non-vector HTTP routes — this is the bring-up surface the
local dev harness promises, not feature parity.
"""

from ..registry import Patch


def _index(original):
    # Local dev / linux bring-up does not bind a vector index. Returning
    # ``None`` matches the upstream default when Pinecone credentials are
    # unset; vector ops fail closed at request time (which is acceptable for
    # a bring-up harness; production targets wire Qdrant or pgvector
    # through their own patches).
    return None


def patches():
    return [
        Patch(
            name='vector.qdrant-index',
            module='database.vector_db',
            attribute='index',
            build=_index,
            # Skip in dev / local bring-up: no vector authority is wired.
            # Production self-host targets will replace this seam with a
            # Qdrant or pgvector binding (out of scope for local bring-up).
            applies_to=lambda row: False,
            reason='local dev bring-up skips vector authority; production targets wire their own backend',
        )
    ]
