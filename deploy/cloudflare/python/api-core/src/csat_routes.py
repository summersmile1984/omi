"""Product CSAT configuration and create-only ratings, owned by App D1."""

import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from brand_runtime import load_brand_runtime
from internal_auth import decode_context

router = APIRouter()
PLATFORMS = {"macos", "windows", "ios", "android"}


class CsatRatingRequest(BaseModel):
    platform: str
    app_version: str = ""
    score: int
    comment: str | None = None
    revision: int = 0


def normalize_config(raw, brand_name):
    # Match backend/database/csat.py's public normalization contract. Only
    # missing default copy uses the deployment brand; admin-authored copy wins.
    raw = raw if isinstance(raw, dict) else {}

    def text(field, fallback):
        value = raw.get(field)
        return value.strip() if isinstance(value, str) and value.strip() else fallback

    def integer(field, default, low, high):
        try:
            value = int(raw.get(field))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    return {
        "enabled": raw.get("enabled") is not False,
        "title": text("title", f"How would you rate {brand_name} Desktop?"),
        "body": text("body", ""),
        "thank_you_text": text("thank_you_text", "Thank you!"),
        "refer_cta_text": text("refer_cta_text", f"Enjoying {brand_name}? Give a friend a free month."),
        "question_threshold": integer("question_threshold", 3, 1, 50),
        "comment_max_score": integer("comment_max_score", 3, 1, 5),
        "revision": integer("revision", 0, 0, 1_000_000_000),
    }


def principal(request):
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


async def product_config(env):
    row = await env.APP_DB.prepare("SELECT config_json FROM cf_csat_config WHERE id = 'product'").first()
    raw = json.loads(row["config_json"]) if row else None
    return normalize_config(raw, load_brand_runtime(env).display_name)


@router.get("/v1/csat/config")
async def get_config(request: Request, platform: str = "macos"):
    if not principal(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return await product_config(request.scope["env"])
    except Exception:
        return JSONResponse({"error": "csat configuration unavailable"}, status_code=503)


@router.post("/v1/csat/ratings", status_code=201)
async def submit_rating(payload: CsatRatingRequest, request: Request):
    context = principal(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if payload.platform not in PLATFORMS:
        raise HTTPException(status_code=400, detail=f"platform must be one of {sorted(PLATFORMS)}")
    if not 1 <= payload.score <= 5:
        raise HTTPException(status_code=400, detail="score must be between 1 and 5")
    if payload.revision < 0:
        raise HTTPException(status_code=400, detail="revision must be >= 0")
    uid = str(context["uid"])
    rating_id = f"{payload.platform}_{uid}"
    env = request.scope["env"]
    try:
        config = await product_config(env)
        comment = (payload.comment or "").strip()[:500] if payload.score <= config["comment_max_score"] else ""
        created = (
            await env.APP_DB.prepare(
                "INSERT INTO cf_csat_ratings (id, uid, platform, app_version, score, comment, revision, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(uid, platform) DO NOTHING RETURNING id"
            )
            .bind(
                rating_id,
                uid,
                payload.platform,
                payload.app_version.strip()[:32],
                payload.score,
                comment,
                payload.revision,
                int(time.time()),
            )
            .first()
        )
    except Exception:
        return JSONResponse({"error": "csat rating unavailable"}, status_code=503)
    return JSONResponse({"id": rating_id, "created": bool(created)}, status_code=201 if created else 409)
