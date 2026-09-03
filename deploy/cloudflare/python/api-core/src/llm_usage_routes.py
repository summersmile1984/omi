"""D1-backed LLM usage aggregation and desktop cost reporting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from internal_auth import decode_context

router = APIRouter()

MAX_TOKEN_COUNTER = 9_007_199_254_740_991
MAX_COST_USD = 1_000_000_000.0


class RecordLlmUsageBucketRequest(BaseModel):
    model_config = {"extra": "ignore", "allow_inf_nan": False}

    input_tokens: int = Field(default=0, ge=0, le=MAX_TOKEN_COUNTER)
    output_tokens: int = Field(default=0, ge=0, le=MAX_TOKEN_COUNTER)
    cache_read_tokens: int = Field(default=0, ge=0, le=MAX_TOKEN_COUNTER)
    cache_write_tokens: int = Field(default=0, ge=0, le=MAX_TOKEN_COUNTER)
    total_tokens: int = Field(default=0, ge=0, le=MAX_TOKEN_COUNTER)
    cost_usd: float = Field(default=0.0, ge=0.0, le=MAX_COST_USD)
    account: str = Field(default="omi", max_length=100)


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _cutoff_day(days: int, now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return (current - timedelta(days=days)).strftime("%Y-%m-%d")


def _feature_payload(row: dict[str, object]) -> dict[str, object]:
    input_tokens = int(row.get("input_tokens") or 0)
    output_tokens = int(row.get("output_tokens") or 0)
    return {
        "feature": str(row.get("feature") or ""),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "call_count": int(row.get("call_count") or 0),
    }


async def _feature_rows(env: object, uid: str, days: int) -> list[dict[str, object]]:
    result = (
        await env.APP_DB.prepare(
            "SELECT feature, SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, SUM(call_count) AS call_count "
            "FROM cf_llm_usage_daily WHERE uid = ? AND usage_kind = 'feature' "
            "AND usage_day >= ? GROUP BY feature"
        )
        .bind(uid, _cutoff_day(days))
        .all()
    )
    raw_rows = result.get("results", []) if isinstance(result, dict) else []
    rows = [_feature_payload(row) for row in raw_rows if isinstance(row, dict)]
    rows.sort(key=lambda row: (-int(row["total_tokens"]), str(row["feature"])))
    return rows


@router.get("/v1/users/me/llm-usage")
async def get_llm_usage(request: Request, days: int = Query(default=30, ge=1, le=365)):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        rows = await _feature_rows(request.scope["env"], str(context["uid"]), days)
    except Exception:
        return JSONResponse({"error": "llm usage unavailable"}, status_code=503)
    summary = {
        str(row["feature"]): {
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "call_count": row["call_count"],
        }
        for row in sorted(rows, key=lambda row: str(row["feature"]))
    }
    return {"summary": summary, "top_features": rows[:5], "period_days": days}


@router.get("/v1/users/me/llm-usage/top-features")
async def get_llm_top_features(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=3, ge=1, le=10),
):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        rows = await _feature_rows(request.scope["env"], str(context["uid"]), days)
    except Exception:
        return JSONResponse({"error": "llm usage unavailable"}, status_code=503)
    return rows[:limit]


@router.post("/v1/users/me/llm-usage")
async def record_llm_usage_bucket(request: Request, payload: RecordLlmUsageBucketRequest):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    now = int(time.time())
    usage_day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    try:
        await request.scope["env"].APP_DB.prepare(
            "INSERT INTO cf_llm_usage_daily "
            "(uid, usage_day, usage_kind, feature, model, account, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, total_tokens, cost_usd, call_count, updated_at) "
            "VALUES (?, ?, 'bucket', 'desktop_chat', '', ?, ?, ?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(uid, usage_day, usage_kind, feature, model, account) DO UPDATE SET "
            "input_tokens = input_tokens + excluded.input_tokens, "
            "output_tokens = output_tokens + excluded.output_tokens, "
            "cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens, "
            "cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens, "
            "total_tokens = total_tokens + excluded.total_tokens, "
            "cost_usd = cost_usd + excluded.cost_usd, "
            "call_count = call_count + 1, updated_at = excluded.updated_at"
        ).bind(
            str(context["uid"]),
            usage_day,
            payload.account,
            payload.input_tokens,
            payload.output_tokens,
            payload.cache_read_tokens,
            payload.cache_write_tokens,
            payload.total_tokens,
            payload.cost_usd,
            now,
        ).run()
    except Exception:
        return JSONResponse({"error": "llm usage unavailable"}, status_code=503)
    return {"status": "ok"}


@router.get("/v1/users/me/llm-usage/total")
async def get_total_llm_cost(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        row = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total_cost_usd "
                "FROM cf_llm_usage_daily WHERE uid = ? AND usage_kind = 'bucket' "
                "AND feature = 'desktop_chat'"
            )
            .bind(str(context["uid"]))
            .first()
        )
        if not isinstance(row, dict):
            raise RuntimeError("llm usage total returned no row")
        total = round(float(row.get("total_cost_usd") or 0.0), 6)
    except Exception:
        return JSONResponse({"error": "llm usage unavailable"}, status_code=503)
    return {"total_cost_usd": total}


__all__ = [
    "RecordLlmUsageBucketRequest",
    "get_llm_top_features",
    "get_llm_usage",
    "get_total_llm_cost",
    "record_llm_usage_bucket",
    "router",
]
