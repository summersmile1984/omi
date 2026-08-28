"""D1-owned public app reviews for the Cloudflare marketplace projection."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from internal_auth import decode_context

router = APIRouter()

MAX_APP_ID_LENGTH = 256
MAX_REVIEWER_UID_LENGTH = 256
MAX_REVIEW_TEXT_LENGTH = 10_000
MAX_REVIEW_USERNAME_LENGTH = 256
MAX_REVIEW_RESPONSE_LENGTH = 10_000
MAX_REVIEW_BODY_BYTES = 64_000
MAX_REVIEW_APP_IDS = 500
MAX_REVIEW_ROWS = 5_000
MAX_APP_PAYLOAD_BYTES = 500_000


class ReviewAppRequest(BaseModel):
    model_config = {"extra": "ignore"}

    score: float
    review: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT_LENGTH)
    username: str | None = Field(default=None, max_length=MAX_REVIEW_USERNAME_LENGTH)
    response: str | None = Field(default=None, max_length=MAX_REVIEW_RESPONSE_LENGTH)


class ReplyToReviewRequest(BaseModel):
    model_config = {"extra": "ignore"}

    reviewer_uid: str = Field(min_length=1, max_length=MAX_REVIEWER_UID_LENGTH)
    response: str = Field(min_length=1, max_length=MAX_REVIEW_RESPONSE_LENGTH)


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


async def _bounded_json(request: Request) -> object:
    body_reader = getattr(request, "body", None)
    if callable(body_reader):
        raw = await body_reader()
        if len(raw) > MAX_REVIEW_BODY_BYTES:
            raise ValueError("request body exceeds size limit")
        return json.loads(raw)
    body = await request.json()
    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_REVIEW_BODY_BYTES:
        raise ValueError("request body exceeds size limit")
    return body


def _flag(value: object) -> bool:
    return value is True or value == 1 or (isinstance(value, str) and value.strip().lower() in {"1", "true"})


def _iso(value: object) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid review timestamp")


def _review(row: dict[str, object]) -> dict[str, object]:
    app_id = row.get("app_id")
    reviewer_uid = row.get("reviewer_uid")
    review_text = row.get("review_text")
    username = row.get("username")
    response = row.get("response")
    if (
        not isinstance(app_id, str)
        or not isinstance(reviewer_uid, str)
        or not isinstance(review_text, str)
        or not isinstance(username, str)
        or not isinstance(response, str)
        or len(review_text) > MAX_REVIEW_TEXT_LENGTH
        or len(username) > MAX_REVIEW_USERNAME_LENGTH
        or len(response) > MAX_REVIEW_RESPONSE_LENGTH
    ):
        raise ValueError("invalid app review")
    try:
        score = float(row.get("score"))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid app review")
    if not math.isfinite(score) or score < 0 or score > 5:
        raise ValueError("invalid app review")
    rated_at = _iso(row.get("rated_at"))
    if rated_at is None:
        raise ValueError("invalid app review")
    return {
        "uid": reviewer_uid,
        "rated_at": rated_at,
        "score": score,
        "review": review_text,
        "username": username,
        "response": response,
        "responded_at": _iso(row.get("responded_at")),
    }


async def hydrate_app_reviews(
    env: object,
    apps: list[dict[str, object]],
    *,
    current_uid: str | None = None,
) -> None:
    app_ids = list(dict.fromkeys(str(app.get("id") or "") for app in apps))
    if not app_ids:
        return
    if len(app_ids) > MAX_REVIEW_APP_IDS or any(not app_id or len(app_id) > MAX_APP_ID_LENGTH for app_id in app_ids):
        raise ValueError("invalid app review projection")
    placeholders = ", ".join("?" for _ in app_ids)
    result = (
        await env.APP_DB.prepare(
            "SELECT app_id, reviewer_uid, score, review_text, username, response, rated_at, responded_at "
            f"FROM cf_app_reviews WHERE app_id IN ({placeholders}) "
            "ORDER BY app_id ASC, rated_at DESC, reviewer_uid ASC LIMIT ?"
        )
        .bind(*app_ids, MAX_REVIEW_ROWS + 1)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    if len(rows) > MAX_REVIEW_ROWS:
        raise ValueError("app review projection exceeds size limit")
    by_app: dict[str, list[dict[str, object]]] = {app_id: [] for app_id in app_ids}
    own_review: dict[str, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid app review projection")
        projected = _review(row)
        app_id = str(row["app_id"])
        if projected["review"]:
            by_app.setdefault(app_id, []).append(projected)
        if current_uid is not None and projected["uid"] == current_uid:
            own_review[app_id] = projected
    for app in apps:
        app_id = str(app.get("id") or "")
        app["reviews"] = by_app.get(app_id, [])
        app["user_review"] = own_review.get(app_id)


async def _reviewable_app(env: object, app_id: str) -> dict[str, str] | None:
    row = (
        await env.APP_DB.prepare(
            "SELECT id, owner_uid, approved, disabled, data_json FROM cf_app_catalog WHERE id = ? LIMIT 1"
        )
        .bind(app_id)
        .first()
    )
    if not isinstance(row, dict) or not _flag(row.get("approved")) or _flag(row.get("disabled")):
        return None
    raw = row.get("data_json")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_APP_PAYLOAD_BYTES:
        raise ValueError("invalid app catalog owner")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("invalid app catalog owner")
    owner_uid = row.get("owner_uid")
    if (
        not isinstance(payload, dict)
        or _flag(payload.get("private"))
        or not isinstance(owner_uid, str)
        or not owner_uid
        or len(owner_uid) > MAX_REVIEWER_UID_LENGTH
    ):
        raise ValueError("invalid app catalog owner")
    name = payload.get("name")
    return {"id": app_id, "owner_uid": owner_uid, "name": name if isinstance(name, str) else "App"}


async def _existing_review(env: object, app_id: str, reviewer_uid: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(
            "SELECT app_id, reviewer_uid, score, review_text, username, response, rated_at, responded_at "
            "FROM cf_app_reviews WHERE app_id = ? AND reviewer_uid = ? LIMIT 1"
        )
        .bind(app_id, reviewer_uid)
        .first()
    )
    return row if isinstance(row, dict) else None


def _score(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("invalid score")
    return max(0.0, min(5.0, value))


def _aggregate_statement(env: object, app_id: str, now: int):
    return env.APP_DB.prepare(
        "UPDATE cf_app_catalog SET "
        "rating_avg = (SELECT AVG(score) FROM cf_app_reviews WHERE app_id = ?), "
        "rating_count = (SELECT COUNT(*) FROM cf_app_reviews WHERE app_id = ?), updated_at = ? WHERE id = ?"
    ).bind(app_id, app_id, now, app_id)


async def _save_review(
    env: object,
    app_id: str,
    reviewer_uid: str,
    *,
    score: float,
    review_text: str,
    username: str,
    response: str,
    rated_at: int,
    responded_at: int | None,
) -> None:
    now = int(time.time())
    upsert = env.APP_DB.prepare(
        "INSERT INTO cf_app_reviews "
        "(app_id, reviewer_uid, score, review_text, username, response, rated_at, updated_at, responded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(app_id, reviewer_uid) DO UPDATE SET score = excluded.score, "
        "review_text = excluded.review_text, username = excluded.username, response = excluded.response, "
        "rated_at = excluded.rated_at, updated_at = excluded.updated_at, responded_at = excluded.responded_at"
    ).bind(app_id, reviewer_uid, score, review_text, username, response, rated_at, now, responded_at)
    await env.APP_DB.batch([upsert, _aggregate_statement(env, app_id, now)])


@router.post("/v1/apps/review")
async def create_app_review(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    app_id = request.query_params.get("app_id")
    if not isinstance(app_id, str) or not app_id or len(app_id) > MAX_APP_ID_LENGTH:
        return JSONResponse({"error": "invalid app id"}, status_code=400)
    try:
        payload = ReviewAppRequest.model_validate(await _bounded_json(request))
        score = _score(payload.score)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid app review"}, status_code=400)
    try:
        app = await _reviewable_app(request.scope["env"], app_id)
    except Exception:
        return JSONResponse({"error": "app reviews unavailable"}, status_code=503)
    if app is None:
        return JSONResponse({"detail": "App not found"}, status_code=404)
    uid = str(context["uid"])
    if app["owner_uid"] == uid:
        return JSONResponse({"detail": "You are not authorized to review your own app"}, status_code=403)
    now = int(time.time())
    try:
        await _save_review(
            request.scope["env"],
            app_id,
            uid,
            score=score,
            review_text=payload.review or "",
            username=payload.username or "",
            response=payload.response or "",
            rated_at=now,
            responded_at=None,
        )
    except Exception:
        return JSONResponse({"error": "app reviews unavailable"}, status_code=503)
    return {"status": "ok"}


@router.patch("/v1/apps/{app_id}/review")
async def update_app_review(request: Request, app_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not app_id or len(app_id) > MAX_APP_ID_LENGTH:
        return JSONResponse({"detail": "App not found"}, status_code=404)
    try:
        payload = ReviewAppRequest.model_validate(await _bounded_json(request))
        score = _score(payload.score)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid app review"}, status_code=400)
    try:
        app = await _reviewable_app(request.scope["env"], app_id)
    except Exception:
        return JSONResponse({"error": "app reviews unavailable"}, status_code=503)
    if app is None:
        return JSONResponse({"detail": "App not found"}, status_code=404)
    uid = str(context["uid"])
    if app["owner_uid"] == uid:
        return JSONResponse({"detail": "You are not authorized to review your own app"}, status_code=403)
    try:
        existing = await _existing_review(request.scope["env"], app_id, uid)
        if existing is None:
            return JSONResponse({"detail": "Review not found"}, status_code=404)
        await _save_review(
            request.scope["env"],
            app_id,
            uid,
            score=score,
            review_text=payload.review or "",
            username=payload.username if payload.username is not None else str(existing.get("username") or ""),
            response=str(existing.get("response") or ""),
            rated_at=int(existing["rated_at"]),
            responded_at=int(existing["responded_at"]) if existing.get("responded_at") is not None else None,
        )
    except Exception:
        return JSONResponse({"error": "app reviews unavailable"}, status_code=503)
    return {"status": "ok"}


@router.patch("/v1/apps/{app_id}/review/reply")
async def reply_to_app_review(request: Request, app_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not app_id or len(app_id) > MAX_APP_ID_LENGTH:
        return JSONResponse({"detail": "App not found"}, status_code=404)
    try:
        payload = ReplyToReviewRequest.model_validate(await _bounded_json(request))
        if not payload.response.strip():
            raise ValueError("empty response")
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid app review reply"}, status_code=422)
    try:
        app = await _reviewable_app(request.scope["env"], app_id)
    except Exception:
        return JSONResponse({"error": "app reviews unavailable"}, status_code=503)
    if app is None:
        return JSONResponse({"detail": "App not found"}, status_code=404)
    uid = str(context["uid"])
    if app["owner_uid"] != uid:
        return JSONResponse({"detail": "You are not authorized to reply to this app review"}, status_code=403)
    try:
        existing = await _existing_review(request.scope["env"], app_id, payload.reviewer_uid)
        if existing is None:
            return JSONResponse({"detail": "Review not found"}, status_code=404)
        now = int(time.time())
        await request.scope["env"].APP_DB.prepare(
            "UPDATE cf_app_reviews SET response = ?, responded_at = ?, updated_at = ? "
            "WHERE app_id = ? AND reviewer_uid = ?"
        ).bind(payload.response, now, now, app_id, payload.reviewer_uid).run()
    except Exception:
        return JSONResponse({"error": "app reviews unavailable"}, status_code=503)
    return {"status": "ok"}


@router.get("/v1/apps/{app_id}/reviews")
async def get_app_reviews(request: Request, app_id: str):
    if not app_id or len(app_id) > MAX_APP_ID_LENGTH:
        return []
    app = {"id": app_id}
    try:
        await hydrate_app_reviews(request.scope["env"], [app])
    except Exception:
        return JSONResponse({"error": "app reviews unavailable"}, status_code=503)
    return app["reviews"]
