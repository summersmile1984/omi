"""Public app catalog reads backed by an explicit D1 projection.

The legacy catalog is backed by Firestore/Redis and also owns private apps,
reviews, install mutations, subscriptions, and MCP credentials. This module
only serves approved public records that have been imported through the
whitelisted backfill tool. A malformed projection fails closed instead of
serving a partially trusted catalog.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()

MAX_APP_RESULTS = 500
MAX_APP_PAYLOAD_BYTES = 500_000


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _include_reviews(request: Request) -> bool | JSONResponse:
    raw = request.query_params.get("include_reviews")
    if raw is None:
        return False
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


async def _read_public_apps(request: Request, *, popular: bool, include_reviews: bool):
    env = request.scope["env"]
    clause = "AND is_popular = 1" if popular else ""
    try:
        result = await env.APP_DB.prepare(
            "SELECT id, approved, disabled, is_popular, installs, rating_avg, rating_count, data_json "
            f"FROM cf_app_catalog WHERE approved = 1 AND disabled = 0 {clause} "
            "ORDER BY is_popular DESC, installs DESC, id ASC LIMIT ?"
        ).bind(MAX_APP_RESULTS).all()
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
        if app is None:  # persona apps are intentionally absent from public catalog reads.
            continue
        apps.append(app)
    return apps


@router.get("/v1/approved-apps")
async def get_approved_apps(request: Request):
    include_reviews = _include_reviews(request)
    if isinstance(include_reviews, JSONResponse):
        return include_reviews
    return await _read_public_apps(request, popular=False, include_reviews=include_reviews)


@router.get("/v1/apps/popular")
async def get_popular_apps(request: Request):
    if not _auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    include_reviews = _include_reviews(request)
    if isinstance(include_reviews, JSONResponse):
        return include_reviews
    return await _read_public_apps(request, popular=True, include_reviews=include_reviews)
