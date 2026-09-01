"""Workers AI model selection for the Cloudflare-native deployment.

The legacy implementation ranked OpenAI/Gemini models through Artificial
Analysis. That made an unrelated upstream API key look like a requirement for
the native deployment. Cloudflare clients use one deterministic Workers AI
model instead; the D1 row is retained only as a small, shared cache so clients
see a stable selection during a rollout.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()

AUTO_MODEL_TTL_SECONDS = 24 * 60 * 60
DEFAULT_WORKERS_AI_MODEL = "@cf/meta/llama-3.2-3b-instruct"
WORKERS_AI_ATTRIBUTION = "https://developers.cloudflare.com/workers-ai/"


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _workers_ai_model(env: object) -> str:
    configured = str(getattr(env, "WORKERS_AI_CHAT_MODEL", "") or "").strip()
    return configured or DEFAULT_WORKERS_AI_MODEL


async def _load_cached_pick(env: object, now: float) -> dict[str, object] | None:
    database = getattr(env, "APP_DB", None)
    if database is None:
        return None
    try:
        row = await database.prepare(
            "SELECT provider, detail_json, updated_at FROM cf_auto_model_pick WHERE id = 1"
        ).first()
    except Exception:
        return None
    if not isinstance(row, dict):
        return None
    try:
        updated_at = float(row["updated_at"])
        detail = json.loads(str(row["detail_json"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(row.get("provider"), str) or not isinstance(detail, dict):
        return None
    if now - updated_at > AUTO_MODEL_TTL_SECONDS:
        return None
    return {"provider": row["provider"], "updated_at": updated_at, "detail": detail}


async def _save_pick(env: object, provider: str, detail: dict[str, Any], updated_at: float) -> None:
    database = getattr(env, "APP_DB", None)
    if database is None:
        return
    try:
        await database.prepare(
            "INSERT INTO cf_auto_model_pick (id, provider, detail_json, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET provider = excluded.provider, detail_json = excluded.detail_json, "
            "updated_at = excluded.updated_at"
        ).bind(provider, json.dumps(detail, ensure_ascii=True), updated_at).run()
    except Exception:
        return


async def _refresh_pick(env: object) -> tuple[str, dict[str, Any]]:
    model = _workers_ai_model(env)
    return "workers-ai", {"model": model, "reason": "workers-ai-native"}


@router.get("/v1/auto/model-pick")
async def auto_model_pick(request: Request):
    if not _auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    now = time.time()
    cached = await _load_cached_pick(env, now)
    if cached is None or cached.get("provider") != "workers-ai":
        provider, detail = await _refresh_pick(env)
        cached = {"provider": provider, "updated_at": now, "detail": detail}
        await _save_pick(env, provider, detail, now)
    return {**cached, "attribution": WORKERS_AI_ATTRIBUTION}
