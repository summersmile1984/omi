"""D1-backed public marketplace grouping for ``GET /v2/apps``.

This is a read-only catalog projection.  It deliberately does not expose
private prompts, reviews, payment identifiers, MCP tokens, or user state, and
does not replace the legacy app authority for writes or external integrations.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app_catalog_routes import _APP_CATEGORIES
from app_projection_routes import _flag, _public_app

router = APIRouter()

MAX_RESULTS = 500
MAX_OFFSET = 100_000
MAX_LIMIT = 100

GROUP_CAPABILITIES = [
    {"title": "Featured", "id": "popular"},
    {"title": "Integrations", "id": "external_integration"},
    {"title": "Chat Assistants", "id": "chat"},
    {"title": "Summary Apps", "id": "memories"},
    {"title": "Realtime Notifications", "id": "proactive_notification"},
    {"title": "Tasks", "id": "tasks"},
]

BASE_CATEGORY_MAPPING = {
    "personality-emulation": "productivity-tools",
    "education-and-learning": "productivity-tools",
    "productivity-and-organization": "productivity-tools",
    "utilities-and-tools": "productivity-tools",
    "financial": "productivity-tools",
    "shopping-and-commerce": "productivity-tools",
    "news-and-information": "productivity-tools",
    "conversation-analysis": "personal-wellness",
    "communication-improvement": "personal-wellness",
    "emotional-and-mental-support": "personal-wellness",
    "health-and-wellness": "personal-wellness",
    "safety-and-security": "personal-wellness",
    "other": "personal-wellness",
    "social-and-relationships": "social-entertainment",
    "entertainment-and-fun": "social-entertainment",
    "travel-and-exploration": "social-entertainment",
}
CHAT_CATEGORY_OVERRIDES = {
    "personality-emulation": "personality-clone",
    "education-and-learning": "productivity-lifestyle",
    "productivity-and-organization": "productivity-lifestyle",
    "utilities-and-tools": "productivity-lifestyle",
    "financial": "productivity-lifestyle",
    "shopping-and-commerce": "productivity-lifestyle",
    "news-and-information": "productivity-lifestyle",
    "conversation-analysis": "productivity-lifestyle",
    "communication-improvement": "productivity-lifestyle",
    "emotional-and-mental-support": "productivity-lifestyle",
    "health-and-wellness": "productivity-lifestyle",
    "safety-and-security": "productivity-lifestyle",
    "other": "productivity-lifestyle",
}


def _query(request: Request, name: str) -> str | None:
    params = getattr(request, "query_params", None)
    value = params.get(name) if params is not None else None
    return value if isinstance(value, str) else None


def _parse_params(request: Request) -> tuple[str | None, str | None, int, int, bool] | JSONResponse:
    capability = _query(request, "capability") or None
    category = _query(request, "category") or None
    try:
        offset = int(_query(request, "offset") or 0)
        limit = int(_query(request, "limit") or 20)
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    if offset < 0 or offset > MAX_OFFSET or limit < 1 or limit > MAX_LIMIT:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    raw_reviews = _query(request, "include_reviews")
    include_reviews = False
    if raw_reviews is not None:
        normalized = raw_reviews.strip().lower()
        if normalized in {"true", "1"}:
            include_reviews = True
        elif normalized not in {"false", "0"}:
            return JSONResponse({"error": "invalid include_reviews"}, status_code=400)
    if capability and category:
        return JSONResponse({"error": "capability and category are mutually exclusive"}, status_code=400)
    return capability, category, offset, limit, include_reviews


def _include_reviews(request: Request, *, default: bool) -> bool | JSONResponse:
    raw = _query(request, "include_reviews")
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    return JSONResponse({"error": "invalid include_reviews"}, status_code=400)


def _score(app: dict[str, object]) -> float:
    try:
        rating = max(0.0, min(5.0, float(app.get("rating_avg") or 0)))
        count = max(0, int(app.get("rating_count") or 0))
        installs = max(0, int(app.get("installs") or 0))
        return ((rating / 5.0) ** 2) * math.log1p(count) * math.sqrt(math.log1p(installs))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _safe_external_integration(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    allowed = {
        "triggers_on",
        "webhook_url",
        "setup_completed_url",
        "setup_instructions_file_path",
        "is_instructions_url",
        "app_home_url",
        "chat_tools_manifest_url",
        "chat_messages_enabled",
        "chat_messages_target",
        "chat_messages_notify",
    }
    return {key: nested for key, nested in value.items() if key in allowed}


def _item(app: dict[str, object]) -> dict[str, object]:
    result = {
        key: app.get(key)
        for key in (
            "id",
            "name",
            "description",
            "image",
            "category",
            "author",
            "capabilities",
            "approved",
            "status",
            "private",
            "installs",
            "rating_avg",
            "rating_count",
            "is_paid",
            "price",
        )
    }
    result["enabled"] = False
    result["external_integration"] = _safe_external_integration(app.get("external_integration"))
    return result


def _is_notification(app: dict[str, object]) -> bool:
    caps = set(app.get("capabilities") or [])
    if "proactive_notification" in caps:
        return True
    external = app.get("external_integration")
    return "external_integration" in caps and isinstance(external, dict) and not external.get("auth_steps") and "chat" not in caps and "memories" not in caps


def _capability(app: dict[str, object]) -> str | None:
    caps = set(app.get("capabilities") or [])
    if _is_notification(app):
        return "proactive_notification"
    external = app.get("external_integration")
    has_auth = isinstance(external, dict) and bool(external.get("auth_steps"))
    if "external_integration" in caps and has_auth:
        return "external_integration"
    if "chat" in caps and not ("external_integration" in caps and has_auth):
        return "chat"
    if "memories" in caps and "chat" not in caps and not ("external_integration" in caps and has_auth):
        return "memories"
    return None


def _sorted(apps: list[dict[str, object]], *, popular: bool = False) -> list[dict[str, object]]:
    if popular:
        return sorted(apps, key=lambda app: (-int(app.get("installs") or 0), str(app.get("id") or "")))
    return sorted(apps, key=lambda app: (-_score(app), str(app.get("id") or "")))


def _pagination(total: int, offset: int, limit: int, category: str | None = None) -> dict[str, object]:
    has_next = offset + limit < total
    result: dict[str, object] = {
        "total": total,
        "count": max(0, min(limit, total - offset)),
        "offset": offset,
        "limit": limit,
        "hasNext": has_next,
        "hasPrevious": offset > 0,
    }
    if category:
        base = f"/v2/apps?category={category}"
        result["links"] = {
            "next": f"{base}&offset={offset + limit}&limit={limit}" if has_next else None,
            "previous": f"{base}&offset={max(offset - limit, 0)}&limit={limit}" if offset > 0 else None,
        }
    return result


async def _read_apps(request: Request, include_reviews: bool) -> list[dict[str, object]] | JSONResponse:
    try:
        result = await request.scope["env"].APP_DB.prepare(
            "SELECT id, approved, disabled, is_popular, installs, rating_avg, rating_count, data_json "
            "FROM cf_app_catalog WHERE approved = 1 AND disabled = 0 "
            "ORDER BY is_popular DESC, installs DESC, id ASC LIMIT ?"
        ).bind(MAX_RESULTS).all()
    except Exception:
        return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    apps: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict) or not _flag(row.get("approved")) or _flag(row.get("disabled")):
            continue
        try:
            app = _public_app(row, include_reviews)
        except (TypeError, ValueError, OverflowError):
            return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
        if app is None or _flag(app.get("private")):
            continue
        apps.append(app)
    return apps


@router.get("/v2/apps")
async def get_apps_v2(request: Request):
    params = _parse_params(request)
    if isinstance(params, JSONResponse):
        return params
    capability, category, offset, limit, include_reviews = params
    apps = await _read_apps(request, include_reviews)
    if isinstance(apps, JSONResponse):
        return apps

    if capability:
        filtered = [app for app in apps if (_flag(app.get("is_popular")) if capability == "popular" else _capability(app) == capability)]
        sorted_apps = _sorted(filtered, popular=capability == "popular")
        return {
            "data": [_item(app) for app in sorted_apps[offset : offset + limit]],
            "pagination": _pagination(len(sorted_apps), offset, limit, capability),
            "capability": {
                "id": capability,
                "title": next((item["title"] for item in GROUP_CAPABILITIES if item["id"] == capability), capability.title().replace("_", " ")),
            },
        }

    if category:
        filtered = [app for app in apps if str(app.get("category") or "") == category]
        sorted_apps = _sorted(filtered)
        return {
            "data": [_item(app) for app in sorted_apps[offset : offset + limit]],
            "pagination": _pagination(len(sorted_apps), offset, limit, category),
            "category": {
                "id": category,
                "title": next((item["title"] for item in _APP_CATEGORIES if item["id"] == category), category.replace("-", " ").title()),
            },
        }

    popular_ids = {str(app.get("id")) for app in apps if _flag(app.get("is_popular"))}
    grouped: dict[str, list[dict[str, object]]] = {}
    for app in apps:
        app_id = str(app.get("id") or "")
        if app_id in popular_ids:
            grouped.setdefault("popular", []).append(app)
            continue
        app_capability = _capability(app)
        if app_capability:
            grouped.setdefault(app_capability, []).append(app)
    groups = []
    for capability_item in GROUP_CAPABILITIES:
        capability_id = capability_item["id"]
        entries = grouped.get(capability_id, [])
        if not entries:
            continue
        ordered = _sorted(entries, popular=capability_id == "popular")
        groups.append(
            {
                "capability": capability_item,
                "data": [_item(app) for app in ordered[offset : offset + limit]],
                "pagination": _pagination(len(ordered), offset, limit, capability_id),
            }
        )
    return {
        "groups": groups,
        "meta": {
            "capabilities": GROUP_CAPABILITIES,
            "groupCount": len(groups),
            "limit": limit,
            "offset": offset,
        },
    }


def _master_category(capability_id: str, original: object) -> str:
    category = str(original or "other")
    mapping = {**BASE_CATEGORY_MAPPING, **CHAT_CATEGORY_OVERRIDES} if capability_id == "chat" else BASE_CATEGORY_MAPPING
    return mapping.get(category, "productivity-lifestyle" if capability_id == "chat" else "personal-wellness")


def _master_categories(capability_id: str) -> list[dict[str, str]]:
    if capability_id == "chat":
        return [
            {"title": "Personality Clones", "id": "personality-clone"},
            {"title": "Productivity & Lifestyle", "id": "productivity-lifestyle"},
            {"title": "Social & Entertainment", "id": "social-entertainment"},
        ]
    return [
        {"title": "Productivity & Tools", "id": "productivity-tools"},
        {"title": "Personal & Lifestyle", "id": "personal-wellness"},
        {"title": "Social & Entertainment", "id": "social-entertainment"},
    ]


@router.get("/v2/apps/capability/{capability_id}/grouped")
async def get_capability_apps_grouped(request: Request, capability_id: str):
    include_reviews = _include_reviews(request, default=True)
    if isinstance(include_reviews, JSONResponse):
        return include_reviews
    apps = await _read_apps(request, include_reviews)
    if isinstance(apps, JSONResponse):
        return apps
    if capability_id == "popular":
        filtered = [app for app in apps if _flag(app.get("is_popular"))]
    else:
        filtered = [app for app in apps if _capability(app) == capability_id]
    grouped: dict[str, list[dict[str, object]]] = {}
    for app in filtered:
        grouped.setdefault(_master_category(capability_id, app.get("category")), []).append(app)
    master_categories = _master_categories(capability_id)
    titles = {item["id"]: item["title"] for item in master_categories}
    ordered_ids = [item["id"] for item in master_categories]
    ordered_ids.extend(key for key in grouped if key not in ordered_ids)
    groups = []
    for category_id in ordered_ids:
        entries = grouped.get(category_id, [])
        if not entries:
            continue
        groups.append(
            {
                "category": {
                    "id": category_id,
                    "title": titles.get(category_id, category_id.replace("-", " ").title()),
                },
                "data": [_item(app) for app in _sorted(entries)],
                "count": len(entries),
            }
        )
    capability_title = next(
        (item["title"] for item in GROUP_CAPABILITIES if item["id"] == capability_id),
        capability_id.title().replace("_", " "),
    )
    return {
        "groups": groups,
        "capability": {"id": capability_id, "title": capability_title},
        "meta": {"totalApps": len(filtered), "groupCount": len(groups)},
    }
