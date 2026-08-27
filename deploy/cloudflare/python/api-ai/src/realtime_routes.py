"""API-first desktop realtime session minting and usage accounting.

The legacy endpoint uses synchronous HTTP clients, a Firestore write, and a
thread executor. The Worker variant keeps the client contract while using the
Workers fetch FFI and a small D1 projection. Provider keys and ephemeral token
values never enter logs or durable storage.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, StrictInt, StrictStr, ValidationError

from internal_auth import decode_context

try:
    from workers import fetch as worker_fetch
except ModuleNotFoundError as error:  # CPython unit tests do not provide Pyodide's `js` module.
    if error.name != "js":
        raise
    worker_fetch = None  # type: ignore[assignment]

router = APIRouter()

OPENAI_REALTIME_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
GEMINI_AUTH_TOKENS_URL = "https://generativelanguage.googleapis.com/v1alpha/auth_tokens"
OPENAI_REALTIME_MODEL = "gpt-realtime-2"
GEMINI_LIVE_MODEL = "models/gemini-3.1-flash-live-preview"
MAX_PROVIDER_BODY_BYTES = 64_000
MAX_USAGE_TOKENS = 100_000_000


class MintRequest(BaseModel):
    provider: StrictStr


class UsageReport(BaseModel):
    provider: StrictStr
    model: StrictStr = ""
    input_text_tokens: StrictInt = 0
    input_audio_tokens: StrictInt = 0
    input_cached_tokens: StrictInt = 0
    output_text_tokens: StrictInt = 0
    output_audio_tokens: StrictInt = 0
    context_plan_id: StrictStr = ""
    stable_cache_identity: StrictStr = ""
    dynamic_context_identity: StrictStr = ""
    context_cache_replaced: bool = False


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _error(
    status_code: int,
    reason: str,
    message: str,
    provider: str | None = None,
    code: str | None = None,
    upstream_status_code: int | None = None,
    retryable: bool = False,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": message,
        "reason": reason,
        "backend_route": "/v2/realtime/session",
        "retryable": retryable,
    }
    if provider is not None:
        body["provider"] = provider
    if code is not None:
        body["code"] = code
    if upstream_status_code is not None:
        body["upstream_status_code"] = upstream_status_code
    return JSONResponse(status_code=status_code, content=body)


def _provider_error(
    provider: str,
    status_code: int,
    payload: object,
) -> JSONResponse:
    parsed = payload if isinstance(payload, dict) else {}
    nested = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
    code_value = nested.get("code") or nested.get("status") or parsed.get("code")
    code = str(code_value) if isinstance(code_value, (str, int, float)) and str(code_value) else None
    message_value = nested.get("message") or parsed.get("message")
    message = message_value if isinstance(message_value, str) else "provider realtime mint failed"
    lower = f"{code or ''} {message}".lower()
    if status_code == 429 or "quota" in lower:
        reason = "provider_quota_exceeded"
    elif status_code in (401, 403) or any(
        value in lower for value in ("invalid api key", "api key not valid", "authentication", "permission denied")
    ):
        reason = "provider_auth_failed"
    elif status_code >= 500:
        reason = "provider_mint_unavailable"
    else:
        reason = "provider_mint_rejected"
    return _error(
        status_code,
        reason,
        message,
        provider,
        code,
        status_code,
        status_code == 429 or status_code >= 500,
    )


async def _post_provider(
    url: str,
    provider: str,
    headers: dict[str, str],
    body: dict[str, object],
) -> tuple[dict[str, object] | None, JSONResponse | None]:
    if worker_fetch is None:
        return None, _error(502, "provider_mint_transport_error", "worker fetch is unavailable", retryable=True)
    encoded = json.dumps(body, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PROVIDER_BODY_BYTES:
        return None, _error(502, "provider_mint_transport_error", "provider request is too large", retryable=False)
    try:
        response = await worker_fetch(
            url,
            method="POST",
            headers={**headers, "content-type": "application/json"},
            body=encoded,
        )
        status = int(response.status)
    except (OSError, TypeError, ValueError, AttributeError):
        return None, _error(502, "provider_mint_transport_error", "provider realtime mint unavailable", retryable=True)
    try:
        payload = await response.json()
    except (TypeError, ValueError, AttributeError):
        payload = {}
    if status < 200 or status >= 300:
        return None, _provider_error(provider, status, payload)
    if not isinstance(payload, dict):
        return None, _error(
            502,
            "provider_mint_transport_error",
            "provider mint response was not an object",
            provider,
            retryable=True,
        )
    return payload, None


def _expires_at(raw: object) -> str | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (str, int, float)):
        return str(raw)
    return None


async def _record_session(env: object, uid: str, token: str, provider: str, model: str, expires_at: str | None) -> None:
    database = getattr(env, "APP_DB", None)
    if database is None:
        return
    try:
        await database.prepare(
            "INSERT INTO cf_realtime_sessions "
            "(uid, token_hash, provider, model, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(uid, token_hash) DO UPDATE SET provider = excluded.provider, model = excluded.model, "
            "expires_at = excluded.expires_at, created_at = excluded.created_at"
        ).bind(
            uid,
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
            provider,
            model,
            expires_at,
            int(time.time()),
        ).run()
    except Exception:
        # Session issuance must not fail because an audit projection is down.
        return


@router.post("/v2/realtime/session")
async def mint_realtime_session(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = MintRequest.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return _error(400, "bad_provider", 'provider must be "openai" or "gemini"')
    provider = payload.provider.strip().lower()
    if provider not in {"openai", "gemini"}:
        return _error(400, "bad_provider", 'provider must be "openai" or "gemini"')

    env = request.scope["env"]
    uid = str(context["uid"])
    if provider == "openai":
        key = str(getattr(env, "OPENAI_API_KEY", "") or "").strip()
        if not key:
            return _error(503, "provider_not_configured", "OpenAI realtime is not configured", "OpenAI", retryable=True)
        data, error = await _post_provider(
            getattr(env, "OPENAI_REALTIME_CLIENT_SECRETS_URL", OPENAI_REALTIME_CLIENT_SECRETS_URL),
            provider,
            {"authorization": f"Bearer {key}"},
            {"session": {"type": "realtime", "model": OPENAI_REALTIME_MODEL}},
        )
        if error:
            return error
        token = data.get("value") if data else None
        if not isinstance(token, str) or not token:
            return _error(
                502,
                "provider_mint_transport_error",
                "openai mint: no client secret in response",
                "OpenAI",
                retryable=True,
            )
        expires_at = _expires_at(data.get("expires_at") if data else None)
        await _record_session(env, uid, token, provider, OPENAI_REALTIME_MODEL, expires_at)
        result: dict[str, object] = {"provider": provider, "token": token}
        if expires_at is not None:
            result["expires_at"] = expires_at
        return JSONResponse(result)

    key = str(getattr(env, "GEMINI_API_KEY", "") or "").strip()
    if not key:
        return _error(503, "provider_not_configured", "Gemini realtime is not configured", "Gemini", retryable=True)
    now = time.time()
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 2 * 60))
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 30 * 60))
    data, error = await _post_provider(
        getattr(env, "GEMINI_AUTH_TOKENS_URL", GEMINI_AUTH_TOKENS_URL) + f"?key={key}",
        provider,
        {},
        {"uses": 1, "expireTime": expires_at, "newSessionExpireTime": start},
    )
    if error:
        return error
    token = data.get("name") if data else None
    if not isinstance(token, str) or not token:
        return _error(
            502,
            "provider_mint_transport_error",
            "gemini mint: no token name in response",
            "Gemini",
            retryable=True,
        )
    await _record_session(env, uid, token, provider, GEMINI_LIVE_MODEL, expires_at)
    return JSONResponse({"provider": provider, "token": token, "expires_at": expires_at})


def _token_count(value: int) -> int:
    return min(max(value, 0), MAX_USAGE_TOKENS)


def _usage_cost(report: UsageReport) -> float:
    rates = (4.0, 32.0, 0.4, 24.0, 64.0) if report.provider.lower() == "openai" else (0.75, 3.0, 0.075, 4.5, 12.0)
    values = (
        _token_count(report.input_text_tokens),
        _token_count(report.input_audio_tokens),
        _token_count(report.input_cached_tokens),
        _token_count(report.output_text_tokens),
        _token_count(report.output_audio_tokens),
    )
    return sum(value * rate for value, rate in zip(values, rates)) / 1_000_000


@router.post("/v2/realtime/usage", status_code=204)
async def report_realtime_usage(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        report = UsageReport.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid realtime usage report"}, status_code=400)
    provider = report.provider.strip().lower()
    if provider not in {"openai", "gemini"}:
        return JSONResponse({"error": "unsupported realtime provider"}, status_code=400)
    total = sum(
        _token_count(value)
        for value in (
            report.input_text_tokens,
            report.input_audio_tokens,
            report.output_text_tokens,
            report.output_audio_tokens,
        )
    )
    cached = _token_count(report.input_cached_tokens)
    total += cached
    if total <= 0:
        return Response(status_code=204)
    database = getattr(request.scope["env"], "APP_DB", None)
    if database is None:
        return Response(status_code=204)
    now = int(time.time())
    usage_date = time.strftime("%Y-%m-%d", time.gmtime(now))
    cost_micros = int(round(_usage_cost(report) * 1_000_000))
    try:
        await database.prepare(
            "INSERT INTO cf_realtime_usage "
            "(uid, usage_date, input_text_tokens, input_audio_tokens, input_cached_tokens, output_text_tokens, "
            "output_audio_tokens, total_tokens, cost_micros, call_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(uid, usage_date) DO UPDATE SET "
            "input_text_tokens = input_text_tokens + excluded.input_text_tokens, "
            "input_audio_tokens = input_audio_tokens + excluded.input_audio_tokens, "
            "input_cached_tokens = input_cached_tokens + excluded.input_cached_tokens, "
            "output_text_tokens = output_text_tokens + excluded.output_text_tokens, "
            "output_audio_tokens = output_audio_tokens + excluded.output_audio_tokens, "
            "total_tokens = total_tokens + excluded.total_tokens, cost_micros = cost_micros + excluded.cost_micros, "
            "call_count = call_count + 1, updated_at = excluded.updated_at"
        ).bind(
            str(context["uid"]),
            usage_date,
            _token_count(report.input_text_tokens),
            _token_count(report.input_audio_tokens),
            cached,
            _token_count(report.output_text_tokens),
            _token_count(report.output_audio_tokens),
            total,
            cost_micros,
            now,
        ).run()
    except Exception:
        return Response(status_code=502)
    return Response(status_code=204)
