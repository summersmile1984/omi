"""D1-backed marketplace grouping and owner search for ``GET /v2/apps``.

Public reads deliberately exclude private prompts, payment identifiers, MCP
tokens, and user state. Authenticated ``my_apps`` reads use the D1 owner column
to include the caller's pending/private records created by the Jobs Worker.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app_catalog_routes import _APP_CATEGORIES
from app_projection_routes import _flag, _public_app
from app_review_routes import hydrate_app_reviews
from internal_auth import decode_context

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


def _item(
    app: dict[str, object],
    *,
    enabled: bool = False,
    include_reviews: bool = False,
) -> dict[str, object]:
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
    result["enabled"] = enabled
    result["external_integration"] = _safe_external_integration(app.get("external_integration"))
    if include_reviews:
        result["reviews"] = app.get("reviews", [])
        result["user_review"] = app.get("user_review")
    return result


def _is_notification(app: dict[str, object]) -> bool:
    caps = set(app.get("capabilities") or [])
    if "proactive_notification" in caps:
        return True
    external = app.get("external_integration")
    return (
        "external_integration" in caps
        and isinstance(external, dict)
        and not external.get("auth_steps")
        and "chat" not in caps
        and "memories" not in caps
    )


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


async def _read_apps(
    request: Request, include_reviews: bool, *, owner_uid: str | None = None
) -> list[dict[str, object]] | JSONResponse:
    try:
        if owner_uid is None:
            statement = (
                request.scope["env"]
                .APP_DB.prepare(
                    "SELECT id, owner_uid, approved, disabled, is_popular, installs, rating_avg, rating_count, data_json "
                    "FROM cf_app_catalog WHERE approved = 1 AND disabled = 0 "
                    "ORDER BY is_popular DESC, installs DESC, id ASC LIMIT ?"
                )
                .bind(MAX_RESULTS)
            )
        else:
            statement = (
                request.scope["env"]
                .APP_DB.prepare(
                    "SELECT id, owner_uid, approved, disabled, is_popular, installs, rating_avg, rating_count, data_json "
                    "FROM cf_app_catalog WHERE owner_uid = ? "
                    "ORDER BY updated_at DESC, id ASC LIMIT ?"
                )
                .bind(owner_uid, MAX_RESULTS)
            )
        result = await statement.all()
    except Exception:
        return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    apps: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        owned = owner_uid is not None and row.get("owner_uid") == owner_uid
        if owner_uid is not None and not owned:
            return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
        if not owned and (not _flag(row.get("approved")) or _flag(row.get("disabled"))):
            continue
        try:
            app = _public_app(row, include_reviews)
        except (TypeError, ValueError, OverflowError):
            return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
        if app is None or (not owned and _flag(app.get("private"))):
            continue
        apps.append(app)
    if include_reviews:
        context = decode_context(
            request.headers.get("x-omi-auth-context"),
            request.headers.get("x-omi-internal-signature"),
            getattr(request.scope["env"], "INTERNAL_ASSERTION_SECRET", None),
        )
        try:
            await hydrate_app_reviews(
                request.scope["env"],
                apps,
                current_uid=str(context["uid"]) if context else None,
            )
        except Exception:
            return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
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
        filtered = [
            app
            for app in apps
            if (_flag(app.get("is_popular")) if capability == "popular" else _capability(app) == capability)
        ]
        sorted_apps = _sorted(filtered, popular=capability == "popular")
        return {
            "data": [_item(app, include_reviews=include_reviews) for app in sorted_apps[offset : offset + limit]],
            "pagination": _pagination(len(sorted_apps), offset, limit, capability),
            "capability": {
                "id": capability,
                "title": next(
                    (item["title"] for item in GROUP_CAPABILITIES if item["id"] == capability),
                    capability.title().replace("_", " "),
                ),
            },
        }

    if category:
        filtered = [app for app in apps if str(app.get("category") or "") == category]
        sorted_apps = _sorted(filtered)
        return {
            "data": [_item(app, include_reviews=include_reviews) for app in sorted_apps[offset : offset + limit]],
            "pagination": _pagination(len(sorted_apps), offset, limit, category),
            "category": {
                "id": category,
                "title": next(
                    (item["title"] for item in _APP_CATEGORIES if item["id"] == category),
                    category.replace("-", " ").title(),
                ),
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
                "data": [_item(app, include_reviews=include_reviews) for app in ordered[offset : offset + limit]],
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
                "data": [_item(app, include_reviews=include_reviews) for app in _sorted(entries)],
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


def _search_bool(request: Request, name: str) -> bool | None | JSONResponse:
    raw = _query(request, name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    return JSONResponse({"error": f"invalid {name}"}, status_code=400)


def _name_match_tier(app: dict[str, object], query: str) -> int:
    name = str(app.get("name") or "").lower()
    if name == query:
        return 0
    if name.startswith(query):
        return 1
    return 2


@router.get("/v2/apps/search")
async def search_apps(request: Request):
    context = decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(request.scope["env"], "INTERNAL_ASSERTION_SECRET", None),
    )
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        offset = int(_query(request, "offset") or 0)
        limit = int(_query(request, "limit") or 20)
        rating = float(_query(request, "rating")) if _query(request, "rating") is not None else None
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid search filters"}, status_code=400)
    if offset < 0 or offset > MAX_OFFSET or limit < 1 or limit > MAX_LIMIT:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    if rating is not None and (not math.isfinite(rating) or rating < 0 or rating > 5):
        return JSONResponse({"error": "invalid rating"}, status_code=400)
    my_apps = _search_bool(request, "my_apps")
    installed_apps = _search_bool(request, "installed_apps")
    if isinstance(my_apps, JSONResponse) or isinstance(installed_apps, JSONResponse):
        return my_apps if isinstance(my_apps, JSONResponse) else installed_apps
    query = _query(request, "q")
    if query is not None and len(query) > 512:
        return JSONResponse({"error": "query too long"}, status_code=400)
    category = _query(request, "category")
    capability = _query(request, "capability")
    sort = _query(request, "sort")
    if sort is not None and len(sort) > 32:
        return JSONResponse({"error": "invalid sort"}, status_code=400)

    uid = str(context["uid"])
    apps = await _read_apps(
        request,
        include_reviews=False,
        owner_uid=uid if my_apps else None,
    )
    if isinstance(apps, JSONResponse):
        return apps
    try:
        enabled_result = (
            await request.scope["env"]
            .APP_DB.prepare("SELECT app_id FROM cf_user_enabled_apps WHERE uid = ?")
            .bind(uid)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "enabled apps unavailable"}, status_code=503)
    enabled_rows = enabled_result.get("results", []) if isinstance(enabled_result, dict) else []
    enabled_ids = {
        str(row["app_id"]) for row in enabled_rows if isinstance(row, dict) and isinstance(row.get("app_id"), str)
    }
    if installed_apps:
        apps = [app for app in apps if str(app.get("id") or "") in enabled_ids]
    if category:
        apps = [app for app in apps if str(app.get("category") or "") == category]
    if capability:
        apps = [
            app
            for app in apps
            if (_flag(app.get("is_popular")) if capability == "popular" else _capability(app) == capability)
        ]
    if query and query.strip():
        normalized_query = query.strip().lower()
        apps = [
            app
            for app in apps
            if normalized_query in str(app.get("name") or "").lower()
            or normalized_query in str(app.get("description") or "").lower()
        ]
    if rating is not None:
        apps = [app for app in apps if float(app.get("rating_avg") or 0) >= rating]
    if sort == "rating_desc":
        apps = sorted(apps, key=lambda app: (-float(app.get("rating_avg") or 0), str(app.get("id") or "")))
    elif sort == "rating_asc":
        apps = sorted(apps, key=lambda app: (float(app.get("rating_avg") or 0), str(app.get("id") or "")))
    elif sort == "name_desc":
        apps = sorted(apps, key=lambda app: str(app.get("name") or "").lower(), reverse=True)
    elif sort == "name_asc":
        apps = sorted(apps, key=lambda app: (str(app.get("name") or "").lower(), str(app.get("id") or "")))
    elif sort in {"installs", "installs_desc"}:
        apps = sorted(apps, key=lambda app: (-int(app.get("installs") or 0), str(app.get("id") or "")))
    elif query and query.strip():
        normalized_query = query.strip().lower()
        apps = sorted(
            apps,
            key=lambda app: (
                _name_match_tier(app, normalized_query),
                -int(app.get("installs") or 0),
                str(app.get("id") or ""),
            ),
        )
    else:
        apps = sorted(apps, key=lambda app: (str(app.get("name") or "").lower(), str(app.get("id") or "")))
    return {
        "data": [_item(app, enabled=str(app.get("id") or "") in enabled_ids) for app in apps[offset : offset + limit]],
        "pagination": _pagination(len(apps), offset, limit),
        "filters": {
            "query": query,
            "category": category,
            "rating": rating,
            "capability": capability,
            "sort": sort or "name",
            "my_apps": my_apps,
            "installed_apps": installed_apps,
        },
    }
