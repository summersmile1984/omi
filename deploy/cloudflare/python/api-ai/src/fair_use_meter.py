"""Exact, idempotent D1 speech metering for Cloudflare transcription routes."""

from __future__ import annotations

import hashlib
import math
import time

MAX_SOURCE_ID_CHARS = 256
MAX_SPEECH_MS = 604_800_000
SOURCE_KINDS = frozenset({"realtime", "sync_fresh", "sync_backfill", "custom_stt"})


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _intervals(value: object) -> list[tuple[int, int]]:
    payload = value if isinstance(value, dict) else {}
    candidates = payload.get("segments")
    if not isinstance(candidates, list) or not candidates:
        candidates = payload.get("words")
    if not isinstance(candidates, list):
        return []

    intervals: list[tuple[int, int]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        text = candidate.get("text", candidate.get("word"))
        if not isinstance(text, str) or not text.strip():
            continue
        start = _number(candidate.get("start"))
        end = _number(candidate.get("end"))
        if start is None or end is None or start < 0 or end <= start:
            continue
        start_ms = round(start * 1000)
        end_ms = round(end * 1000)
        if end_ms <= start_ms or end_ms - start_ms > MAX_SPEECH_MS:
            continue
        intervals.append((start_ms, end_ms))
    return intervals


def speech_ms_from_transcription(value: object) -> int:
    """Return the union of provider speech intervals, excluding overlap/silence."""
    intervals = sorted(_intervals(value))
    if not intervals:
        return 0
    total = 0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        total += current_end - current_start
        current_start, current_end = start, end
    total += current_end - current_start
    return min(total, MAX_SPEECH_MS)


def content_source_id(namespace: str, content: bytes, idempotency_key: str | None = None) -> str:
    identity = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
    content_digest = hashlib.sha256(content).digest()
    digest_input = identity.encode("utf-8") + b"\0" + content_digest if identity else content_digest
    digest = hashlib.sha256(digest_input).hexdigest()
    source_id = f"{namespace}:{digest}"
    if len(source_id) > MAX_SOURCE_ID_CHARS:
        raise ValueError("fair-use source id is too long")
    return source_id


async def record_fair_use_usage(
    env: object,
    *,
    uid: str,
    source_kind: str,
    source_id: str,
    speech_ms: int,
    dg_ms: int = 0,
    occurred_at: int | None = None,
    revision: int = 1,
) -> bool:
    if speech_ms <= 0:
        return False
    if source_kind not in SOURCE_KINDS:
        raise ValueError("invalid fair-use source kind")
    if not 1 <= len(source_id) <= MAX_SOURCE_ID_CHARS:
        raise ValueError("invalid fair-use source id")
    if not 0 < speech_ms <= MAX_SPEECH_MS or not 0 <= dg_ms <= MAX_SPEECH_MS:
        raise ValueError("invalid fair-use duration")
    if not 1 <= revision <= 2_147_483_647:
        raise ValueError("invalid fair-use revision")
    database = getattr(env, "APP_DB", None)
    if database is None:
        raise RuntimeError("fair-use D1 binding is unavailable")
    now = int(time.time())
    await database.prepare(
        "INSERT INTO cf_fair_use_usage_sources "
        "(uid, source_kind, source_id, occurred_at, speech_ms, dg_ms, updated_at, revision) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET "
        "speech_ms = excluded.speech_ms, dg_ms = excluded.dg_ms, "
        "updated_at = excluded.updated_at, revision = excluded.revision "
        "WHERE excluded.revision >= cf_fair_use_usage_sources.revision"
    ).bind(
        uid,
        source_kind,
        source_id,
        occurred_at if occurred_at is not None else now,
        speech_ms,
        dg_ms,
        now,
        revision,
    ).run()
    return True
