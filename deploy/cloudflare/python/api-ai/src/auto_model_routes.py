import json
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

try:
    from workers import fetch as worker_fetch
except ModuleNotFoundError as error:  # CPython unit tests do not provide Pyodide's `js` module.
    if error.name != "js":
        raise
    worker_fetch = None  # type: ignore[assignment]

router = APIRouter()

ARTIFICIAL_ANALYSIS_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
ARTIFICIAL_ANALYSIS_ATTRIBUTION = "https://artificialanalysis.ai/"
AUTO_MODEL_TTL_SECONDS = 24 * 60 * 60
QUALITY_WEIGHT = 0.65
SPEED_WEIGHT = 0.35
SPEED_CAP = 250.0
PROXY_MODELS = {
    "geminiFlashLive": "gemini-3-5-flash",
    "gptRealtime2": "gpt-5",
}


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _score(quality: object, speed: object) -> float | None:
    try:
        quality_value = min(max(float(quality), 0.0), 100.0) / 100.0
        speed_value = min(max(float(speed), 0.0), SPEED_CAP) / SPEED_CAP
    except (TypeError, ValueError):
        return None
    return QUALITY_WEIGHT * quality_value + SPEED_WEIGHT * speed_value


def _pick_from_models(models: object) -> tuple[str, dict[str, Any]]:
    if not isinstance(models, list):
        return "geminiFlashLive", {"reason": "Artificial Analysis returned no model list"}
    scores: dict[str, float] = {}
    for provider, slug_substring in PROXY_MODELS.items():
        best: float | None = None
        for model in models:
            if not isinstance(model, dict):
                continue
            slug = str(model.get("slug") or model.get("id") or model.get("name") or "").lower()
            if slug_substring not in slug:
                continue
            evaluations = model.get("evaluations")
            if not isinstance(evaluations, dict):
                continue
            score = _score(
                evaluations.get("artificial_analysis_intelligence_index"),
                model.get("median_output_tokens_per_second"),
            )
            if score is not None and (best is None or score > best):
                best = score
        if best is not None:
            scores[provider] = round(best, 4)
    if not scores:
        return "geminiFlashLive", {"reason": "no matching Artificial Analysis models", "scores": {}}
    provider = max(scores, key=scores.get)
    return provider, {"scores": scores}


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
    key = getattr(env, "ARTIFICIALANALYSIS_API_KEY", None)
    if not key:
        return "geminiFlashLive", {"reason": "no ARTIFICIALANALYSIS_API_KEY; default to Gemini"}
    if worker_fetch is None:
        return "geminiFlashLive", {"reason": "worker fetch is unavailable"}
    try:
        response = await worker_fetch(
            getattr(env, "ARTIFICIALANALYSIS_API_URL", ARTIFICIAL_ANALYSIS_URL),
            method="GET",
            headers={"x-api-key": key, "accept": "application/json"},
        )
        if int(response.status) != 200:
            return "geminiFlashLive", {"reason": "Artificial Analysis unavailable", "status": int(response.status)}
        payload = await response.json()
        return _pick_from_models(payload.get("data") if isinstance(payload, dict) else None)
    except (OSError, TypeError, ValueError):
        return "geminiFlashLive", {"reason": "Artificial Analysis unavailable"}


@router.get("/v1/auto/model-pick")
async def auto_model_pick(request: Request):
    if not _auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    now = time.time()
    cached = await _load_cached_pick(env, now)
    if cached is None:
        provider, detail = await _refresh_pick(env)
        cached = {"provider": provider, "updated_at": now, "detail": detail}
        await _save_pick(env, provider, detail, now)
    return {**cached, "attribution": ARTIFICIAL_ANALYSIS_ATTRIBUTION}
