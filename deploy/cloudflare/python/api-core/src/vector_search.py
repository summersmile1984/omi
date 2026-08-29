"""Cloudflare Vectorize candidate projection helpers.

D1 remains authoritative. Vectorize stores only embeddings plus a hashed tenant
namespace; every candidate ID is mapped through ``cf_vector_projection_state``
and then hydrated from the uid-scoped D1 source table before it can be returned.
"""

from __future__ import annotations

import hashlib
import math
import re
import time

from fallback import record_fallback

VECTOR_MODEL = "@cf/baai/bge-m3"
VECTOR_DIMENSIONS = 1024
MAX_QUERY_CHARS = 4_096
VECTOR_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PROJECTION_KINDS = frozenset({"memory", "action_item", "conversation", "transcript_chunk", "x_post"})
SOURCE_KINDS = frozenset({"memory", "action_item", "conversation", "x_post"})


def vector_namespace(uid: str) -> str:
    return hashlib.sha256(f"omi-vector-namespace\0{uid}".encode()).hexdigest()


def _to_python(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _to_python(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_python(item) for item in value]
    to_py = getattr(value, "to_py", None)
    if callable(to_py):
        try:
            converted = to_py()
        except TypeError:
            converted = None
        if converted is not None and converted is not value:
            return _to_python(converted)
    result: dict[str, object] = {}
    for field in ("data", "count", "matches", "id", "score", "metadata", "namespace"):
        item = getattr(value, field, None)
        if item is not None:
            result[field] = _to_python(item)
    return result


def _embedding_vector(result: object) -> list[float] | None:
    payload = _to_python(result)
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], list):
        return None
    raw = data[0]
    if len(raw) != VECTOR_DIMENSIONS:
        return None
    vector: list[float] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return None
        vector.append(float(value))
    return vector


async def embed_query(env: object, query: str) -> list[float]:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query is too long (max {MAX_QUERY_CHARS} characters)")
    ai = getattr(env, "AI", None)
    if ai is None:
        raise RuntimeError("workers ai is not configured")
    model = getattr(env, "WORKERS_AI_VECTOR_MODEL", VECTOR_MODEL)
    result = await ai.run(model, {"text": [query.strip()]})
    vector = _embedding_vector(result)
    if vector is None:
        raise RuntimeError("workers ai returned invalid embeddings")
    return vector


async def query_vector_ids(
    env: object,
    binding_name: str,
    uid: str,
    vector: list[float],
    *,
    top_k: int,
    created_at_filter: dict[str, object] | None = None,
) -> list[tuple[str, float]]:
    binding = getattr(env, binding_name, None)
    if binding is None:
        raise RuntimeError(f"{binding_name} is not configured")
    options: dict[str, object] = {
        "topK": max(1, min(int(top_k), 100)),
        "namespace": vector_namespace(uid),
        "returnValues": False,
        "returnMetadata": "none",
    }
    if created_at_filter:
        options["filter"] = {"created_at": created_at_filter}
    raw_result = await binding.query(vector, options)
    result = _to_python(raw_result)
    matches = result.get("matches") if isinstance(result, dict) else None
    if not isinstance(matches, list):
        raise RuntimeError("vectorize returned invalid matches")
    candidates: list[tuple[str, float]] = []
    seen: set[str] = set()
    for raw in matches:
        if not isinstance(raw, dict):
            continue
        vector_id = raw.get("id")
        score = raw.get("score")
        if (
            not isinstance(vector_id, str)
            or VECTOR_ID_PATTERN.fullmatch(vector_id) is None
            or vector_id in seen
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
        ):
            continue
        seen.add(vector_id)
        candidates.append((vector_id, float(score)))
    return candidates


async def hydrate_candidate_ids(
    env: object,
    uid: str,
    projection_kind: str,
    matches: list[tuple[str, float]],
    *,
    minimum_score: float | None = None,
) -> list[tuple[str, float]]:
    if projection_kind not in PROJECTION_KINDS:
        raise ValueError("invalid projection kind")
    filtered = [match for match in matches if minimum_score is None or match[1] >= minimum_score]
    if not filtered:
        return []
    vector_ids = [vector_id for vector_id, _ in filtered]
    rows: list[dict[str, object]] = []
    for offset in range(0, len(vector_ids), 100):
        chunk = vector_ids[offset : offset + 100]
        placeholders = ",".join("?" for _ in chunk)
        result = (
            await env.APP_DB.prepare(
                "SELECT vector_id, source_id FROM cf_vector_projection_state "
                f"WHERE uid = ? AND projection_kind = ? AND vector_id IN ({placeholders})"
            )
            .bind(uid, projection_kind, *chunk)
            .all()
        )
        values = result.get("results", []) if isinstance(result, dict) else []
        rows.extend(row for row in values if isinstance(row, dict))
    source_by_vector = {
        str(row["vector_id"]): str(row["source_id"])
        for row in rows
        if isinstance(row.get("vector_id"), str) and isinstance(row.get("source_id"), str)
    }
    hydrated: list[tuple[str, float]] = []
    seen_sources: set[str] = set()
    for vector_id, score in filtered:
        source_id = source_by_vector.get(vector_id)
        if not source_id or source_id in seen_sources:
            continue
        seen_sources.add(source_id)
        hydrated.append((source_id, score))
    return hydrated


def vector_outbox_statement(
    env: object,
    *,
    uid: str,
    source_kind: str,
    source_id: str,
    desired_version: int,
    operation: str,
):
    if source_kind not in SOURCE_KINDS or operation not in {"upsert", "delete"}:
        raise ValueError("invalid vector projection request")
    now = int(time.time())
    return env.APP_DB.prepare(
        "INSERT INTO cf_vector_projection_outbox "
        "(uid, source_kind, source_id, desired_version, operation, attempts, next_attempt_at, last_error, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, NULL, ?, ?) "
        "ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET "
        "desired_version = excluded.desired_version, operation = excluded.operation, attempts = 0, "
        "next_attempt_at = excluded.next_attempt_at, last_error = NULL, updated_at = excluded.updated_at "
        "WHERE excluded.desired_version >= cf_vector_projection_outbox.desired_version"
    ).bind(uid, source_kind, source_id, desired_version, operation, now, now, now)


async def publish_vector_projection(env: object, *, uid: str, source_kind: str, source_id: str) -> None:
    queue = getattr(env, "JOBS", None)
    if queue is None:
        record_fallback(
            component="other",
            from_mode="queue",
            to_mode="scheduled_reconciler",
            reason="dependency_unavailable",
            outcome="degraded",
        )
        return
    digest = hashlib.sha256(f"{uid}\0{source_kind}\0{source_id}".encode()).hexdigest()
    try:
        await queue.send(
            {
                "jobId": f"vector-{digest[:48]}",
                "uid": uid,
                "kind": "vector_project",
                "payload": {"sourceKind": source_kind, "sourceId": source_id},
            }
        )
    except Exception:
        record_fallback(
            component="other",
            from_mode="queue",
            to_mode="scheduled_reconciler",
            reason="dependency_unavailable",
            outcome="recovered",
        )


__all__ = [
    "MAX_QUERY_CHARS",
    "VECTOR_DIMENSIONS",
    "VECTOR_MODEL",
    "embed_query",
    "hydrate_candidate_ids",
    "publish_vector_projection",
    "query_vector_ids",
    "vector_namespace",
    "vector_outbox_statement",
]
