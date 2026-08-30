"""App catalog reads backed by the Cloudflare D1 authority.

Public callers only receive approved marketplace records. Authenticated owners
may also read their own private, pending, or disabled record so Cloudflare-owned
create/update flows do not depend on the retired Firestore authority. A
malformed projection fails closed instead of serving a partially trusted
catalog.
"""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app_review_routes import hydrate_app_reviews
from internal_auth import decode_context

router = APIRouter()

MAX_APP_RESULTS = 500
MAX_APP_PAYLOAD_BYTES = 500_000
MAX_APP_ID_LENGTH = 256


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _include_reviews(request: Request, *, default: bool = False) -> bool | JSONResponse:
    raw = request.query_params.get("include_reviews")
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    return JSONResponse({"error": "invalid include_reviews"}, status_code=400)


def _flag(value: object) -> bool:
    return value is True or value == 1 or (isinstance(value, str) and value.strip().lower() in {"1", "true"})


def _public_app(row: dict[str, object], include_reviews: bool) -> dict[str, object] | None:
    raw = row.get("data_json")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_APP_PAYLOAD_BYTES:
        raise ValueError("invalid app payload")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("invalid app payload")
    if not isinstance(payload, dict):
        raise ValueError("invalid app payload")
    capabilities = payload.get("capabilities", [])
    if not isinstance(capabilities, (list, set, tuple)):
        raise ValueError("invalid app payload")
    if any(not isinstance(value, str) for value in capabilities):
        raise ValueError("invalid app payload")
    if "persona" in capabilities:
        return None

    result = dict(payload)
    result["id"] = str(row.get("id") or result.get("id") or "")
    result["approved"] = bool(row.get("approved"))
    result["rejected"] = not result["approved"]
    result["disabled"] = bool(row.get("disabled"))
    result["is_popular"] = bool(row.get("is_popular"))
    result["installs"] = max(0, int(row.get("installs") or 0))
    result["rating_count"] = max(0, int(row.get("rating_count") or 0))
    if row.get("rating_avg") is not None:
        result["rating_avg"] = float(row["rating_avg"])
    if not include_reviews:
        result.pop("reviews", None)
        result.pop("user_review", None)
    return result


def _strip_owner_only_fields(app: dict[str, object]) -> dict[str, object]:
    result = dict(app)
    for key in (
        "email",
        "memory_prompt",
        "chat_prompt",
        "persona_prompt",
        "payment_product_id",
        "payment_price_id",
        "payment_link_id",
        "money_made",
        "usage_count",
    ):
        result.pop(key, None)
    external = result.get("external_integration")
    if isinstance(external, dict):
        sanitized = dict(external)
        sanitized.pop("mcp_oauth_tokens", None)
        result["external_integration"] = sanitized
    return result


def _owned_persona(row: dict[str, object], uid: str) -> dict[str, object] | None:
    if row.get("owner_uid") != uid:
        return None
    raw = row.get("data_json")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_APP_PAYLOAD_BYTES:
        raise ValueError("invalid persona payload")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("invalid persona payload")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list) or "persona" not in capabilities:
        return None
    result = dict(payload)
    result["id"] = str(row.get("id") or result.get("id") or "")
    result["approved"] = _flag(row.get("approved"))
    result["rejected"] = not result["approved"]
    result["disabled"] = _flag(row.get("disabled"))
    return result


async def _read_public_apps(request: Request, *, popular: bool, include_reviews: bool):
    env = request.scope["env"]
    clause = "AND is_popular = 1" if popular else ""
    try:
        result = (
            await env.APP_DB.prepare(
                "SELECT id, approved, disabled, is_popular, installs, rating_avg, rating_count, data_json "
                f"FROM cf_app_catalog WHERE approved = 1 AND disabled = 0 "
                f"AND COALESCE(json_extract(data_json, '$.private'), 0) NOT IN (1, 'true') {clause} "
                "ORDER BY is_popular DESC, installs DESC, id ASC LIMIT ?"
            )
            .bind(MAX_APP_RESULTS)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "app catalog unavailable"}, status_code=503)

    rows = result.get("results", []) if isinstance(result, dict) else []
    apps = []
    for row in rows:
        if not isinstance(row, dict):
            return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
        # Keep this guard in addition to the SQL predicate so a stale/imported
        # row can never bypass the public visibility boundary.
        if not _flag(row.get("approved")) or _flag(row.get("disabled")):
            continue
        try:
            app = _public_app(row, include_reviews)
        except (TypeError, ValueError, OverflowError):
            return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
        if app is None or _flag(app.get("private")):  # personas/private apps are absent from public reads.
            continue
        apps.append(_strip_owner_only_fields(app))
    if include_reviews:
        context = _auth_context(request)
        try:
            await hydrate_app_reviews(
                env,
                apps,
                current_uid=str(context["uid"]) if context else None,
            )
        except Exception:
            return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    return apps


@router.get("/v1/approved-apps")
async def get_approved_apps(request: Request):
    include_reviews = _include_reviews(request)
    if isinstance(include_reviews, JSONResponse):
        return include_reviews
    return await _read_public_apps(request, popular=False, include_reviews=include_reviews)


@router.get("/v1/personas")
async def get_persona_details(request: Request):
    """Return the authenticated user's Persona from the owner projection."""
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    try:
        row = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT id, owner_uid, approved, disabled, data_json "
                "FROM cf_app_catalog WHERE owner_uid = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT 20"
            )
            .bind(uid)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "persona unavailable"}, status_code=503)
    rows = row.get("results", []) if isinstance(row, dict) else []
    try:
        for candidate in rows:
            if isinstance(candidate, dict):
                persona = _owned_persona(candidate, uid)
                if persona is not None and not persona["disabled"]:
                    return persona
    except (TypeError, ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "persona unavailable"}, status_code=503)
    return JSONResponse({"detail": "Persona not found"}, status_code=404)


@router.get("/v1/apps/popular")
async def get_popular_apps(request: Request):
    if not _auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    include_reviews = _include_reviews(request)
    if isinstance(include_reviews, JSONResponse):
        return include_reviews
    return await _read_public_apps(request, popular=True, include_reviews=include_reviews)


def _decorate_user_state(app: dict[str, object], row: dict[str, object], uid: str) -> None:
    entitled = _flag(row.get("user_entitled"))
    paid = _flag(app.get("is_paid")) or bool(app.get("payment_link") or app.get("payment_link_id"))
    app["is_user_paid"] = entitled
    app["enabled"] = _flag(row.get("user_enabled")) and (entitled if paid else True)
    payment_link = app.get("payment_link")
    if isinstance(payment_link, str) and payment_link:
        separator = "&" if "?" in payment_link else "?"
        app["payment_link"] = f"{payment_link}{separator}client_reference_id=uid_{quote(uid, safe='')}"


@router.get("/v1/apps")
async def get_apps(request: Request):
    """Return the caller's marketplace, owned, and explicitly assigned tester apps."""
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    include_reviews = _include_reviews(request, default=True)
    if isinstance(include_reviews, JSONResponse):
        return include_reviews
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        result = (
            await env.APP_DB.prepare(
                "SELECT c.id, c.owner_uid, c.approved, c.disabled, c.is_popular, c.installs, "
                "c.rating_avg, c.rating_count, c.data_json, "
                "CASE WHEN u.app_id IS NULL THEN 0 ELSE 1 END AS user_enabled, "
                "CASE WHEN s.status IN ('active', 'trialing') AND s.current_period_end > unixepoch() "
                "THEN 1 ELSE 0 END AS user_entitled, "
                "CASE WHEN ta.app_id IS NULL THEN 0 ELSE 1 END AS tester_access "
                "FROM cf_app_catalog c "
                "LEFT JOIN cf_user_enabled_apps u ON u.app_id = c.id AND u.uid = ? "
                "LEFT JOIN cf_app_subscriptions s ON s.app_id = c.id AND s.uid = ? "
                "LEFT JOIN cf_app_tester_access ta ON ta.app_id = c.id AND ta.uid = ? "
                "WHERE c.disabled = 0 AND ("
                "c.owner_uid = ? OR "
                "(c.approved = 1 AND COALESCE(json_extract(c.data_json, '$.private'), 0) NOT IN (1, 'true')) OR "
                "(c.approved = 0 AND ta.app_id IS NOT NULL)) "
                "ORDER BY c.is_popular DESC, c.installs DESC, c.id ASC LIMIT ?"
            )
            .bind(uid, uid, uid, uid, MAX_APP_RESULTS)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "app catalog unavailable"}, status_code=503)

    rows = result.get("results", []) if isinstance(result, dict) else []
    apps: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict) or _flag(row.get("disabled")):
            return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
        owner = isinstance(row.get("owner_uid"), str) and row.get("owner_uid") == uid
        tester = _flag(row.get("tester_access")) and not _flag(row.get("approved"))
        try:
            projected = _public_app(row, include_reviews=False)
        except (TypeError, ValueError, OverflowError):
            return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
        if projected is None:
            continue
        public = _flag(row.get("approved")) and not _flag(projected.get("private"))
        if not owner and not tester and not public:
            return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
        if not owner:
            projected = _strip_owner_only_fields(projected)
        _decorate_user_state(projected, row, uid)
        apps.append(projected)
    if include_reviews:
        try:
            await hydrate_app_reviews(env, apps, current_uid=uid)
        except Exception:
            return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    return apps


@router.get("/v1/apps/tester/check")
async def check_is_tester(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        row = (
            await request.scope["env"]
            .APP_DB.prepare("SELECT uid FROM cf_app_testers WHERE uid = ? LIMIT 1")
            .bind(str(context["uid"]))
            .first()
        )
    except Exception:
        return JSONResponse({"error": "app tester unavailable"}, status_code=503)
    return {"is_tester": isinstance(row, dict)}


@router.get("/v1/apps/{app_id}")
async def get_app(request: Request, app_id: str):
    """Return a public app, or the caller's own non-public app, plus user state."""
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not app_id or len(app_id) > MAX_APP_ID_LENGTH:
        return JSONResponse({"detail": "App not found"}, status_code=404)

    env = request.scope["env"]
    try:
        result = (
            await env.APP_DB.prepare(
                "SELECT c.id, c.owner_uid, c.approved, c.disabled, c.is_popular, c.installs, "
                "c.rating_avg, c.rating_count, c.data_json, "
                "CASE WHEN u.app_id IS NULL THEN 0 ELSE 1 END AS user_enabled, "
                "CASE WHEN s.status IN ('active', 'trialing') AND s.current_period_end > unixepoch() "
                "THEN 1 ELSE 0 END AS user_entitled, "
                "CASE WHEN ta.app_id IS NULL THEN 0 ELSE 1 END AS tester_access "
                "FROM cf_app_catalog c "
                "LEFT JOIN cf_user_enabled_apps u ON u.app_id = c.id AND u.uid = ? "
                "LEFT JOIN cf_app_subscriptions s ON s.app_id = c.id AND s.uid = ? "
                "LEFT JOIN cf_app_tester_access ta ON ta.app_id = c.id AND ta.uid = ? "
                "WHERE c.id = ? LIMIT 1"
            )
            .bind(str(context["uid"]), str(context["uid"]), str(context["uid"]), app_id)
            .first()
        )
    except Exception:
        return JSONResponse({"error": "app catalog unavailable"}, status_code=503)

    if not isinstance(result, dict):
        return JSONResponse({"detail": "App not found"}, status_code=404)
    uid = str(context["uid"])
    owner = isinstance(result.get("owner_uid"), str) and result.get("owner_uid") == uid
    tester = _flag(result.get("tester_access")) and not _flag(result.get("approved"))
    # Retain application-level guards so a malformed adapter or stale row can
    # never expose a private/pending record to a non-owner.
    if str(result.get("id") or "") != app_id:
        return JSONResponse({"detail": "App not found"}, status_code=404)
    try:
        app = _public_app(result, include_reviews=False)
    except (TypeError, ValueError, OverflowError):
        return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    public = (
        app is not None
        and _flag(result.get("approved"))
        and not _flag(result.get("disabled"))
        and not _flag(app.get("private"))
    )
    if app is None or (not owner and not tester and not public) or (tester and _flag(result.get("disabled"))):
        return JSONResponse({"detail": "App not found"}, status_code=404)
    if not owner:
        app = _strip_owner_only_fields(app)
    _decorate_user_state(app, result, uid)
    try:
        await hydrate_app_reviews(env, [app], current_uid=uid)
    except Exception:
        return JSONResponse({"error": "app catalog unavailable"}, status_code=503)
    return app
