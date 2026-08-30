"""D1-backed public trend projections for the Cloudflare profile.

Trends are a global, read-only derived index.  Categories and topic counts are
backfilled into D1 before this route is promoted; the request path never needs
Firestore or a per-user identity.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

_VALID_CATEGORIES = frozenset({"ceo", "company", "software_product", "hardware_product", "ai_product"})
_VALID_TYPES = frozenset({"best", "worst"})


def _iso(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.fromtimestamp(0, timezone.utc).isoformat()


def _count(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


async def _load_rows(env: object) -> list[dict[str, object]]:
    result = await env.APP_DB.prepare(
        "SELECT c.id AS category_id, c.category, c.type, c.created_at, "
        "t.id AS topic_id, t.topic, t.memories_count "
        "FROM cf_trend_categories c "
        "LEFT JOIN cf_trend_topics t ON t.category_id = c.id "
        "ORDER BY c.created_at DESC, c.id ASC, t.memories_count DESC, t.id ASC"
    ).all()
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def _response(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    categories: dict[str, dict[str, object]] = {}
    for row in rows:
        category_id = row.get("category_id")
        category = row.get("category")
        trend_type = row.get("type")
        if (
            not isinstance(category_id, str)
            or not category_id
            or not isinstance(category, str)
            or category not in _VALID_CATEGORIES
            or not isinstance(trend_type, str)
            or trend_type not in _VALID_TYPES
        ):
            continue
        record = categories.get(category_id)
        if record is None:
            record = {
                "id": category_id,
                "category": category,
                "type": trend_type,
                "created_at": _iso(row.get("created_at")),
                "topics": [],
            }
            categories[category_id] = record
        topic_id = row.get("topic_id")
        topic = row.get("topic")
        if not isinstance(topic_id, str) or not topic_id or not isinstance(topic, str) or not topic:
            continue
        topics = record["topics"]
        if isinstance(topics, list):
            topics.append(
                {
                    "id": topic_id,
                    "topic": topic,
                    "memories_count": _count(row.get("memories_count")),
                }
            )
    return list(categories.values())


@router.get("/v1/trends")
async def get_trends(request: Request):
    try:
        payload = _response(await _load_rows(request.scope["env"]))
    except Exception:
        return JSONResponse({"error": "trends unavailable"}, status_code=503)
    return JSONResponse(
        payload,
        headers={"cache-control": "public, max-age=60"},
    )


__all__ = ["router"]
