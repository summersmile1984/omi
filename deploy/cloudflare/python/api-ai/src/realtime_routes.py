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
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, StrictInt, StrictStr, ValidationError

from internal_auth import decode_context
from chat_quota import free_quota_detail, question_reservation_statement, trial_paywall_applies

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
    turn_id: StrictStr = Field(default="", max_length=1024)
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
    provider = report.provider.strip().lower()
    if provider in {"workers-ai", "cloudflare-workers-ai"}:
        # Native Realtime usage is metered by the DO's speech-duration source;
        # this legacy token-shaped endpoint must not invent an external cost.
        return 0.0
    elif provider == "openai":
        rates = (4_000_000, 32_000_000, 400_000, 400_000, 24_000_000, 64_000_000)
    else:
        rates = (750_000, 3_000_000, 750_000, 3_000_000, 4_500_000, 12_000_000)
    # Match upstream client_reported_turn: cache is a subset of the modality
    # counts, attributed text first. Gemini Live publishes no cache discount.
    text = _token_count(report.input_text_tokens)
    audio = _token_count(report.input_audio_tokens)
    cached = _token_count(report.input_cached_tokens)
    cached_text = min(cached, text)
    cached_audio = min(cached - cached_text, audio)
    values = (
        text - cached_text,
        audio - cached_audio,
        cached_text,
        cached_audio,
        _token_count(report.output_text_tokens),
        _token_count(report.output_audio_tokens),
    )
    # Integer micro-USD rate cards and half-up rounding match the server's
    # realtime_turn_cost_micro_usd; binary float/banker's rounding does not.
    micros = (sum(value * rate for value, rate in zip(values, rates)) + 500_000) // 1_000_000
    return micros / 1_000_000


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
    if provider not in {"openai", "gemini", "workers-ai", "cloudflare-workers-ai"}:
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
        return Response(status_code=502)
    env = request.scope["env"]
    uid = str(context["uid"])
    managed = provider in {"openai", "gemini"}
    provider = provider if managed else "workers-ai"
    # Managed hub turns and native speech metadata are different meters. Both
    # native aliases share one identity; the two hub providers share another.
    prefix = "realtime_hub:" if managed else "realtime_speech:"
    identity = hashlib.sha256(report.turn_id.encode()).hexdigest() if report.turn_id else "legacy:" + uuid.uuid4().hex
    key = prefix + identity
    model = (OPENAI_REALTIME_MODEL if provider == "openai" else GEMINI_LIVE_MODEL) if managed else ""
    now = int(time.time())
    cost_micros = int(round(_usage_cost(report) * 1_000_000))
    try:
        statements = []
        if managed:
            statements.append(
                question_reservation_statement(
                    env,
                    uid=uid,
                    idempotency_key=key,
                    message_id=identity,
                    chat_session_id=None,
                    platform="desktop",
                    account_created_at=context.get("accountCreatedAt"),
                    has_byok_keys=False,
                    occurred_at=now,
                    source="desktop_realtime_turn",
                )
            )
        statements.append(
            database.prepare(
                "INSERT INTO cf_realtime_usage_events "
                "(uid, idempotency_key, provider, model, input_text_tokens, input_audio_tokens, input_cached_tokens, "
                "output_text_tokens, output_audio_tokens, total_tokens, cost_micros, occurred_at) "
                "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE ? = 1 OR EXISTS ("
                "SELECT 1 FROM cf_chat_quota_events WHERE uid = ? AND idempotency_key = ? "
                "AND source = 'desktop_realtime_turn') ON CONFLICT(uid, idempotency_key) DO NOTHING"
            ).bind(
                uid,
                key,
                provider,
                model,
                _token_count(report.input_text_tokens),
                _token_count(report.input_audio_tokens),
                cached,
                _token_count(report.output_text_tokens),
                _token_count(report.output_audio_tokens),
                total,
                cost_micros,
                now,
                int(not managed),
                uid,
                key,
            )
        )
        await database.batch(statements)
        receipt = (
            await database.prepare(
                "SELECT 1 AS recorded FROM cf_realtime_usage_events WHERE uid = ? AND idempotency_key = ?"
            )
            .bind(uid, key)
            .first()
        )
        if not isinstance(receipt, dict):
            if not managed:
                raise RuntimeError("native usage receipt unavailable")
            detail = await free_quota_detail(
                env,
                uid,
                force_exhausted=trial_paywall_applies(
                    env,
                    platform="desktop",
                    account_created_at=context.get("accountCreatedAt"),
                    has_byok_keys=False,
                ),
            )
            return JSONResponse({"detail": detail}, status_code=402, headers={"cache-control": "no-store"})
    except Exception:
        return Response(status_code=502)
    return Response(status_code=204)
