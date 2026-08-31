"""Cloudflare-owned Gemini REST adapter with bounded D1/DO admission.

This is intentionally a provider-specific route.  The generic ``/v1/ai``
proxy is OpenAI-compatible and must not be used as a Gemini wire adapter.
Vertex ADC/PT remains a separate, fail-closed provider until a Cloudflare
service-identity contract is available.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from internal_auth import decode_context
from vertex_auth import (
    VertexAuthError,
    access_token as vertex_access_token,
    parse_service_account,
)

try:
    from workers import fetch as worker_fetch
except ModuleNotFoundError as error:  # CPython unit tests do not provide Pyodide's ``js`` module.
    if error.name != "js":
        raise
    worker_fetch = None  # type: ignore[assignment]

router = APIRouter()

MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_CONTENT_ITEMS = 128
MAX_CONTENT_PARTS = 512
MAX_INLINE_MEDIA_PARTS = 16
DEFAULT_DAILY_LIMIT = 1_500
MAX_OUTPUT_TOKENS = 8_192
SERVER_PAID_MAX_OUTPUT_TOKENS = 2_048
VERTEX_DEFAULT_LOCATION = "us-central1"
VERTEX_LOCATION_PATTERN = re.compile(r"^(?:global|[a-z]{2,16}(?:-[a-z0-9]{1,16}){0,3})$")
VERTEX_ACTIONS = frozenset({"generateContent", "streamGenerateContent", "embedContent"})
VERTEX_BASE_HOST_PATTERN = re.compile(r"^[a-z0-9-]+-aiplatform\.googleapis\.com$")
PROVIDER_TIMEOUT_SECONDS = 75
STREAM_IDLE_TIMEOUT_SECONDS = 75
MAX_REQUEST_ID_CHARS = 128
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

ALLOWED_ACTIONS = frozenset({"generateContent", "streamGenerateContent", "embedContent", "batchEmbedContents"})
ALLOWED_MODELS = frozenset(
    {
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
        "gemini-3.1-flash-lite",
        "gemini-embedding-001",
    }
)
FORBIDDEN_QUERY_KEYS = frozenset({"key", "access_token", "oauth_token"})


class GeminiProviderError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 503,
        retryable: bool = True,
        retry_after: int | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


def _json_error(
    request_id: str,
    code: str,
    message: str,
    *,
    status_code: int,
    retryable: bool,
    retry_after: int | None = None,
    provider: str = "ai_studio",
) -> JSONResponse:
    headers = {
        "cache-control": "no-store",
        "x-request-id": request_id,
        "x-omi-request-id": request_id,
        "x-omi-provider": provider,
        "x-omi-error-class": code,
        "x-omi-retryable": "true" if retryable else "false",
    }
    if retry_after is not None:
        headers["retry-after"] = str(max(1, retry_after))
    return JSONResponse(
        {
            "error": code,
            "message": message,
            "request_id": request_id,
            "retryable": retryable,
        },
        status_code=status_code,
        headers=headers,
    )


def _request_id(request: Request, context: Mapping[str, Any]) -> str:
    for name in ("x-omi-request-id", "x-request-id", "idempotency-key"):
        candidate = request.headers.get(name, "").strip()
        if candidate and REQUEST_ID_PATTERN.fullmatch(candidate):
            return candidate[:MAX_REQUEST_ID_CHARS]
    candidate = context.get("requestId")
    if isinstance(candidate, str) and REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate[:MAX_REQUEST_ID_CHARS]
    return uuid.uuid4().hex


def _context(request: Request) -> dict[str, Any] | None:
    state = getattr(request, "state", None)
    context = getattr(state, "auth_context", None)
    if isinstance(context, dict) and isinstance(context.get("uid"), str):
        return context
    env = request.scope.get("env")
    secret = getattr(env, "INTERNAL_ASSERTION_SECRET", None)
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        secret,
    )


def _parse_path(path: str) -> tuple[str, str, str]:
    original = path
    if original.startswith("models/"):
        model_action = original[7:]
    else:
        model_action = ""
    model_part, separator, action = model_action.partition(":")
    if model_part == "gemini-3-flash-preview":
        model_part = "gemini-2.5-flash"
        original = original.replace("gemini-3-flash-preview", model_part, 1)
    if not separator or model_part not in ALLOWED_MODELS or action not in ALLOWED_ACTIONS:
        raise GeminiProviderError(
            "gemini_model_or_action_not_allowed",
            "Gemini model or action is not allowed",
            status_code=403,
            retryable=False,
        )
    return original, model_part, action


def _body_shape(payload: Mapping[str, Any], size: int) -> None:
    contents = payload.get("contents")
    if not isinstance(contents, list):
        return
    if len(contents) > MAX_CONTENT_ITEMS:
        raise GeminiProviderError(
            "gemini_request_too_large", "Gemini request has too many content items", status_code=413, retryable=False
        )
    part_count = 0
    inline_media_count = 0
    for content in contents:
        if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
            continue
        part_count += len(content["parts"])
        for part in content["parts"]:
            if isinstance(part, dict) and ("inlineData" in part or "inline_data" in part):
                inline_media_count += 1
    if part_count > MAX_CONTENT_PARTS:
        raise GeminiProviderError(
            "gemini_request_too_large", "Gemini request has too many content parts", status_code=413, retryable=False
        )
    if inline_media_count > MAX_INLINE_MEDIA_PARTS:
        raise GeminiProviderError(
            "gemini_request_too_large",
            "Gemini request has too many inline media parts",
            status_code=413,
            retryable=False,
        )
    if size > MAX_BODY_BYTES:
        raise GeminiProviderError(
            "gemini_request_too_large", "Gemini request body is too large", status_code=413, retryable=False
        )


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _sanitize_payload(
    body: bytes,
    *,
    action: str,
    max_output_tokens: int,
) -> tuple[bytes, dict[str, Any]]:
    if len(body) > MAX_BODY_BYTES:
        raise GeminiProviderError(
            "gemini_request_too_large", "Gemini request body is too large", status_code=413, retryable=False
        )
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as error:
        raise GeminiProviderError(
            "invalid_request", "Request body must be valid JSON", status_code=400, retryable=False
        ) from error
    if not isinstance(payload, dict):
        raise GeminiProviderError(
            "invalid_request", "Request body must be a JSON object", status_code=400, retryable=False
        )
    _body_shape(payload, len(body))
    # Match the desktop proxy's safety boundary.  These fields are provider
    # policy inputs owned by the server, not caller-controlled routing knobs.
    for key in ("safety_settings", "safetySettings", "cached_content", "cachedContent"):
        payload.pop(key, None)
    contents = payload.get("contents")
    if isinstance(contents, list):
        system_parts: list[Any] = []
        remaining: list[Any] = []
        for content in contents:
            if not isinstance(content, dict):
                remaining.append(content)
                continue
            role = content.setdefault("role", "user")
            if role == "system":
                if isinstance(content.get("parts"), list):
                    system_parts.extend(content["parts"])
            else:
                remaining.append(content)
        payload["contents"] = remaining
        if system_parts:
            instruction = payload.get("systemInstruction") or payload.get("system_instruction")
            if isinstance(instruction, dict) and isinstance(instruction.get("parts"), list):
                instruction["parts"].extend(system_parts)
            else:
                payload["systemInstruction"] = {"parts": system_parts}
    # Gemini accepts candidate count and output budgets in either the legacy
    # snake_case form or the REST camelCase form.  The desktop contract only
    # serves one candidate.  Keep this validation and cap in the Worker so a
    # Cloudflare request cannot bypass the paid-lane cost boundary by sending
    # an otherwise valid provider field the old proxy would have constrained.
    if action not in {"embedContent", "batchEmbedContents"}:
        for key in ("candidate_count", "candidateCount"):
            value = _nonnegative_int(payload.get(key))
            if value is not None and value > 1:
                raise GeminiProviderError(
                    "invalid_request",
                    "candidate_count must be 1 or absent",
                    status_code=400,
                    retryable=False,
                )
        configs = [
            payload[key] for key in ("generation_config", "generationConfig") if isinstance(payload.get(key), dict)
        ]
        if not configs:
            payload["generationConfig"] = {"maxOutputTokens": max_output_tokens}
        for config in configs:
            for key in ("candidate_count", "candidateCount"):
                value = _nonnegative_int(config.get(key))
                if value is not None and value > 1:
                    raise GeminiProviderError(
                        "invalid_request",
                        "candidate_count must be 1 or absent",
                        status_code=400,
                        retryable=False,
                    )
            output_seen = False
            for key in ("max_output_tokens", "maxOutputTokens"):
                value = _nonnegative_int(config.get(key))
                if value is not None:
                    output_seen = True
                    if value > max_output_tokens:
                        config[key] = max_output_tokens
            if not output_seen:
                config["maxOutputTokens"] = max_output_tokens
    return json.dumps(payload, separators=(",", ":")).encode(), payload


def _safe_provider_url(base_url: str, path: str, query: str, *, stream: bool) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise GeminiProviderError(
            "gemini_provider_unavailable", "Gemini provider URL is not configured", retryable=True
        )
    query_items = parse_qsl(query, keep_blank_values=True)
    if any(key.casefold() in FORBIDDEN_QUERY_KEYS for key, _ in query_items):
        raise GeminiProviderError(
            "invalid_request", "Provider credential query parameters are not allowed", status_code=400, retryable=False
        )
    if stream and not any(key == "alt" for key, _ in query_items):
        query_items.append(("alt", "sse"))
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"{parsed.path.rstrip('/')}/{path.lstrip('/')}",
            urlencode(query_items),
            "",
        )
    )


def _vertex_location(env: object) -> str:
    location = str(getattr(env, "GEMINI_VERTEX_LOCATION", VERTEX_DEFAULT_LOCATION)).strip().lower()
    if not VERTEX_LOCATION_PATTERN.fullmatch(location):
        raise GeminiProviderError(
            "gemini_vertex_unavailable",
            "Vertex Gemini location is not configured",
            status_code=503,
            retryable=True,
        )
    return location


def _vertex_project(env: object, service_account_json: str) -> str:
    configured = str(getattr(env, "GEMINI_VERTEX_PROJECT_ID", "")).strip()
    account = parse_service_account(service_account_json, expected_project_id=configured or None)
    if account is None:
        raise GeminiProviderError(
            "gemini_vertex_unavailable",
            "Vertex Gemini service identity is not configured",
            status_code=503,
            retryable=True,
        )
    return account.project_id


def _vertex_auth_failure(error: VertexAuthError) -> GeminiProviderError:
    if error.code == "vertex_auth_timeout":
        return GeminiProviderError(
            "gemini_vertex_auth_timeout",
            "Vertex Gemini credential exchange timed out",
            status_code=504,
            retryable=True,
        )
    if error.code == "vertex_auth_rate_limited":
        return GeminiProviderError(
            "gemini_vertex_auth_rate_limited",
            "Vertex Gemini credential exchange was rate limited",
            status_code=429,
            retryable=True,
            retry_after=30,
        )
    if error.code in {"vertex_auth_rejected", "vertex_auth_invalid_response"}:
        return GeminiProviderError(
            "gemini_vertex_auth_rejected",
            "Vertex Gemini service identity was rejected",
            status_code=503,
            retryable=False,
        )
    return GeminiProviderError(
        "gemini_vertex_auth_unavailable",
        "Vertex Gemini service identity is unavailable",
        status_code=503,
        retryable=True,
    )


def _vertex_provider_url(
    env: object,
    *,
    project: str,
    location: str,
    model: str,
    action: str,
    query: str,
    stream: bool,
) -> str:
    configured = str(getattr(env, "GEMINI_VERTEX_API_BASE_URL", "")).strip()
    if configured:
        base_url = configured
    elif location == "global":
        base_url = "https://aiplatform.googleapis.com/v1"
    else:
        base_url = f"https://{location}-aiplatform.googleapis.com/v1"
    parsed = urlsplit(base_url)
    try:
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError as error:
        raise GeminiProviderError(
            "gemini_vertex_unavailable",
            "Vertex Gemini endpoint is not configured",
            status_code=503,
            retryable=True,
        ) from error
    if (
        parsed.scheme != "https"
        or not hostname
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1"
        or (hostname != "aiplatform.googleapis.com" and not VERTEX_BASE_HOST_PATTERN.fullmatch(hostname))
    ):
        raise GeminiProviderError(
            "gemini_vertex_unavailable",
            "Vertex Gemini endpoint is not configured",
            status_code=503,
            retryable=True,
        )
    provider_action = "predict" if action == "embedContent" else action
    query_items = parse_qsl(query, keep_blank_values=True)
    if any(key.casefold() in FORBIDDEN_QUERY_KEYS for key, _ in query_items):
        raise GeminiProviderError(
            "invalid_request",
            "Provider credential query parameters are not allowed",
            status_code=400,
            retryable=False,
        )
    if stream and not any(key == "alt" for key, _ in query_items):
        query_items.append(("alt", "sse"))
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:{provider_action}",
            urlencode(query_items),
            "",
        )
    )


def _vertex_embedding_request(body: bytes) -> bytes:
    try:
        payload = json.loads(body)
        content = payload["content"]
        text = content["parts"][0]["text"]
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise GeminiProviderError(
            "invalid_request",
            "embedContent requires content.parts[0].text",
            status_code=400,
            retryable=False,
        ) from error
    instance: dict[str, Any] = {"content": text}
    for source, destination in (("taskType", "task_type"), ("title", "title")):
        if source in payload:
            instance[destination] = payload[source]
    return json.dumps({"instances": [instance]}, separators=(",", ":")).encode()


def _vertex_embedding_response(body: bytes) -> bytes:
    try:
        values = json.loads(body)["predictions"][0]["embeddings"]["values"]
    except (KeyError, IndexError, TypeError, ValueError):
        return body
    return json.dumps({"embedding": {"values": values}}, separators=(",", ":")).encode()


def _response_header(response: object, name: str, default: str = "") -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return default
    value = headers.get(name) if hasattr(headers, "get") else None
    return value if isinstance(value, str) and value else default


def _bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    to_py = getattr(value, "to_py", None)
    if callable(to_py):
        converted = to_py()
        if converted is not value:
            return _bytes(converted)
    try:
        return bytes(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return b""


async def _array_buffer(response: object) -> bytes:
    method = getattr(response, "arrayBuffer", None)
    if not callable(method):
        return b""
    return _bytes(await method())


def _usage(payload: Mapping[str, Any]) -> dict[str, int | str] | None:
    raw = payload.get("usageMetadata")
    if not isinstance(raw, dict):
        return None
    names = {
        "prompt_tokens": ("promptTokenCount",),
        "output_tokens": ("candidatesTokenCount",),
        "total_tokens": ("totalTokenCount",),
        "cached_input_tokens": ("cachedContentTokenCount",),
        "reasoning_tokens": ("thoughtsTokenCount",),
        "traffic_type": ("trafficType",),
    }
    result: dict[str, int | str] = {}
    for destination, candidates in names.items():
        for source in candidates:
            value = raw.get(source)
            if destination == "traffic_type":
                if isinstance(value, str) and value in {"PROVISIONED_THROUGHPUT", "ON_DEMAND"}:
                    result[destination] = value
                break
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                result[destination] = value
                break
    return result or None


def _sse_usage(body: bytes) -> dict[str, int | str] | None:
    latest: dict[str, int | str] | None = None
    normalized = body.replace(b"\r\n", b"\n")
    for event in normalized.split(b"\n\n"):
        data = [line[5:].lstrip() for line in event.splitlines() if line.startswith(b"data:")]
        if not data:
            continue
        try:
            payload = json.loads(b"\n".join(data))
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            current = _usage(payload)
            if current:
                latest = current
    return latest


def _cost_micros(env: object, usage: Mapping[str, int | str] | None) -> int | None:
    if not usage:
        return None
    prompt = usage.get("prompt_tokens")
    output = usage.get("output_tokens")
    if not isinstance(prompt, int) or not isinstance(output, int):
        return None
    try:
        input_rate = float(getattr(env, "GEMINI_INPUT_USD_PER_MILLION", ""))
        output_rate = float(getattr(env, "GEMINI_OUTPUT_USD_PER_MILLION", ""))
    except (TypeError, ValueError):
        return None
    if input_rate < 0 or output_rate < 0:
        return None
    return max(0, round((prompt * input_rate + output * output_rate) * 1_000_000 / 1_000_000))


def _daily_limit(env: object) -> int:
    try:
        value = int(getattr(env, "GEMINI_DAILY_LIMIT", DEFAULT_DAILY_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LIMIT
    return value if 1 <= value <= 100_000 else DEFAULT_DAILY_LIMIT


async def _reserve_daily(
    env: object,
    *,
    request_id: str,
    uid: str,
    model: str,
    action: str,
    credential_source: str,
    provider: str,
    now: int,
    account_generation: int,
) -> str:
    database = getattr(env, "APP_DB", None)
    if database is None:
        raise GeminiProviderError("gemini_usage_unavailable", "Gemini usage ledger is unavailable")
    window_start = (now // 86_400) * 86_400
    try:
        initialize = database.prepare(
            "INSERT OR IGNORE INTO cf_gemini_quota_windows "
            "(uid, window_kind, window_start, request_count, updated_at) VALUES (?, 'daily', ?, 0, ?)"
        ).bind(uid, window_start, now)
        reserve = database.prepare(
            "INSERT OR IGNORE INTO cf_gemini_usage_receipts "
            "(request_id, uid, model, action, credential_source, provider, status, account_generation, created_at) "
            "SELECT ?, ?, ?, ?, ?, ?, 'reserved', ?, ? "
            "WHERE NOT EXISTS (SELECT 1 FROM cf_gemini_usage_receipts WHERE request_id = ?) "
            "AND (SELECT request_count FROM cf_gemini_quota_windows "
            "     WHERE uid = ? AND window_kind = 'daily' AND window_start = ?) < ?"
        ).bind(
            request_id,
            uid,
            model,
            action,
            credential_source,
            provider,
            account_generation,
            now,
            request_id,
            uid,
            window_start,
            _daily_limit(env),
        )
        await database.batch([initialize, reserve])
        row = (
            await database.prepare("SELECT uid, status FROM cf_gemini_usage_receipts WHERE request_id = ?")
            .bind(request_id)
            .first()
        )
    except GeminiProviderError:
        raise
    except Exception as error:
        raise GeminiProviderError("gemini_usage_unavailable", "Gemini usage ledger is unavailable") from error
    if not isinstance(row, dict):
        try:
            await database.prepare(
                "INSERT OR IGNORE INTO cf_gemini_usage_receipts "
                "(request_id, uid, model, action, credential_source, provider, status, account_generation, created_at, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?, 'rejected', ?, ?, 'daily_quota_exceeded')"
            ).bind(request_id, uid, model, action, credential_source, provider, account_generation, now).run()
        except Exception as error:
            raise GeminiProviderError("gemini_usage_unavailable", "Gemini usage ledger is unavailable") from error
        raise GeminiProviderError(
            "gemini_daily_quota_exceeded",
            "Gemini daily request limit exceeded",
            status_code=429,
            retryable=False,
            retry_after=max(1, 86_400 - (now - window_start)),
        )
    if row.get("uid") != uid:
        raise GeminiProviderError(
            "gemini_request_conflict", "Gemini request id belongs to another account", status_code=409, retryable=False
        )
    status = row.get("status")
    if status == "reserved":
        return "reserved"
    if status == "rejected":
        raise GeminiProviderError(
            "gemini_daily_quota_exceeded", "Gemini daily request limit exceeded", status_code=429, retryable=False
        )
    raise GeminiProviderError(
        "gemini_request_replayed",
        "Gemini request id has already reached a terminal state",
        status_code=409,
        retryable=False,
    )


async def _settle(
    env: object,
    *,
    request_id: str,
    uid: str,
    status: str,
    usage: Mapping[str, int | str] | None,
    now: int,
    cost_micros: int | None,
    error: str | None = None,
) -> bool:
    database = getattr(env, "APP_DB", None)
    if database is None:
        return False
    try:
        await database.prepare(
            "UPDATE cf_gemini_usage_receipts SET status = ?, prompt_tokens = ?, output_tokens = ?, "
            "total_tokens = ?, cached_input_tokens = ?, reasoning_tokens = ?, traffic_type = ?, "
            "estimated_cost_micros = ?, completed_at = ?, last_error = ? "
            "WHERE request_id = ? AND uid = ? AND status = 'reserved'"
        ).bind(
            status,
            usage.get("prompt_tokens") if usage else None,
            usage.get("output_tokens") if usage else None,
            usage.get("total_tokens") if usage else None,
            usage.get("cached_input_tokens") if usage else None,
            usage.get("reasoning_tokens") if usage else None,
            usage.get("traffic_type") if usage else None,
            cost_micros,
            now,
            error,
            request_id,
            uid,
        ).run()
        return True
    except Exception:
        return False


def _provider_error(status: int) -> tuple[int, str, str, bool, int | None]:
    if status == 429:
        return 429, "gemini_provider_rate_limited", "Gemini provider rate limited the request", True, 30
    if status in {408, 504}:
        return (
            504,
            "gemini_provider_timeout",
            "Gemini provider timed out before returning a terminal response",
            False,
            None,
        )
    if status >= 500:
        return 502, "gemini_provider_unavailable", "Gemini provider returned an unavailable response", True, 5
    return status, "gemini_provider_rejected", "Gemini provider rejected the request", False, None


def _stream_data_event(code: str, request_id: str) -> bytes:
    return f"data: {json.dumps({'error': code, 'request_id': request_id, 'retryable': False}, separators=(',', ':'))}\n\n".encode()


async def _stream_body(
    response: object,
    *,
    env: object,
    uid: str,
    request_id: str,
) -> AsyncIterator[bytes]:
    chunks = bytearray()
    body = getattr(response, "body", None)
    reader_factory = getattr(body, "getReader", None)
    try:
        if callable(reader_factory):
            reader = reader_factory()
            while True:
                try:
                    async with asyncio.timeout(STREAM_IDLE_TIMEOUT_SECONDS):
                        result = await reader.read()
                except TimeoutError:
                    await _settle(
                        env,
                        request_id=request_id,
                        uid=uid,
                        status="failed",
                        usage=None,
                        now=int(time.time()),
                        cost_micros=None,
                        error="stream_timeout",
                    )
                    yield _stream_data_event("gemini_stream_timeout", request_id)
                    return
                done = getattr(result, "done", None)
                value = getattr(result, "value", None)
                if isinstance(result, dict):
                    done = result.get("done")
                    value = result.get("value")
                if done:
                    break
                chunk = _bytes(value)
                if not chunk:
                    continue
                if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                    yield _stream_data_event("gemini_response_too_large", request_id)
                    return
                chunks.extend(chunk)
                yield chunk
        else:
            chunk = await _array_buffer(response)
            if len(chunk) > MAX_RESPONSE_BYTES:
                yield _stream_data_event("gemini_response_too_large", request_id)
                return
            chunks.extend(chunk)
            if chunk:
                yield chunk
        usage = _sse_usage(bytes(chunks))
        await _settle(
            env,
            request_id=request_id,
            uid=uid,
            status="success",
            usage=usage,
            now=int(time.time()),
            cost_micros=_cost_micros(env, usage),
        )
    except Exception:
        await _settle(
            env,
            request_id=request_id,
            uid=uid,
            status="failed",
            usage=None,
            now=int(time.time()),
            cost_micros=None,
            error="stream_transport_error",
        )
        yield _stream_data_event("gemini_stream_unavailable", request_id)


async def _proxy(request: Request, path: str, *, stream_route: bool) -> Response:
    context = _context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    request_id = _request_id(request, context)
    env = request.scope["env"]
    if str(getattr(env, "GEMINI_PROXY_ENABLED", "false")).lower() != "true":
        return _json_error(
            request_id,
            "gemini_proxy_unavailable",
            "Cloudflare Gemini proxy is not enabled",
            status_code=503,
            retryable=True,
        )
    provider = str(getattr(env, "GEMINI_PROXY_PROVIDER", "ai_studio")).strip().lower()
    if provider in {"vertex", "vertex_ai"}:
        provider = "vertex_ai"
    elif provider == "ai_studio":
        provider = "ai_studio"
    else:
        return _json_error(
            request_id,
            "gemini_provider_unavailable",
            "Gemini provider is not configured for Cloudflare",
            status_code=503,
            retryable=True,
        )
    reserved = False
    try:
        normalized_path, model, action = _parse_path(path)
        if stream_route and action != "streamGenerateContent":
            raise GeminiProviderError(
                "invalid_request",
                "Gemini stream route requires streamGenerateContent",
                status_code=400,
                retryable=False,
            )
        body = await request.body()
        byok_key = request.headers.get("x-byok-gemini")
        if context.get("byokActive") is True and not byok_key:
            raise GeminiProviderError(
                "gemini_byok_unavailable", "Gemini BYOK key is unavailable", status_code=403, retryable=False
            )
        credential_source = "byok" if byok_key else "server"
        sanitized_body, _ = _sanitize_payload(
            body,
            action=action,
            max_output_tokens=MAX_OUTPUT_TOKENS if byok_key else SERVER_PAID_MAX_OUTPUT_TOKENS,
        )
        if provider == "vertex_ai":
            if worker_fetch is None:
                raise GeminiProviderError("gemini_provider_unavailable", "Worker fetch is unavailable")
            if byok_key:
                raise GeminiProviderError(
                    "gemini_vertex_byok_unsupported",
                    "Vertex Gemini does not accept request BYOK credentials",
                    status_code=403,
                    retryable=False,
                )
            if action not in VERTEX_ACTIONS or action == "batchEmbedContents":
                raise GeminiProviderError(
                    "gemini_vertex_action_unavailable",
                    "Gemini action is not supported by the configured Vertex provider",
                    status_code=503,
                    retryable=True,
                )
            service_account_json = str(getattr(env, "GEMINI_VERTEX_SERVICE_ACCOUNT_JSON", ""))
            project = _vertex_project(env, service_account_json)
            location = _vertex_location(env)
            try:
                token = await vertex_access_token(
                    service_account_json,
                    worker_fetch,
                    expected_project_id=project,
                )
            except VertexAuthError as error:
                raise _vertex_auth_failure(error) from error
            url = _vertex_provider_url(
                env,
                project=project,
                location=location,
                model=model,
                action=action,
                query=request.url.query,
                stream=stream_route or action == "streamGenerateContent",
            )
            provider_headers = {"authorization": f"Bearer {token}"}
            if action == "embedContent":
                sanitized_body = _vertex_embedding_request(sanitized_body)
        else:
            api_key = byok_key or str(getattr(env, "GEMINI_API_KEY", "")).strip()
            if not api_key:
                raise GeminiProviderError("gemini_provider_unavailable", "Gemini provider is not configured")
            base_url = str(getattr(env, "GEMINI_API_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"))
            url = _safe_provider_url(
                base_url,
                normalized_path,
                request.url.query,
                stream=stream_route or action == "streamGenerateContent",
            )
            if worker_fetch is None:
                raise GeminiProviderError("gemini_provider_unavailable", "Worker fetch is unavailable")
            provider_headers = {"x-goog-api-key": api_key}
        await _reserve_daily(
            env,
            request_id=request_id,
            uid=str(context["uid"]),
            model=model,
            action=action,
            credential_source=credential_source,
            provider=provider,
            now=int(time.time()),
            account_generation=int(context.get("accountGeneration") or 0),
        )
        reserved = True
        try:
            async with asyncio.timeout(PROVIDER_TIMEOUT_SECONDS):
                response = await worker_fetch(
                    url,
                    method="POST",
                    headers={
                        "content-type": "application/json",
                        "accept": (
                            "text/event-stream"
                            if stream_route or action == "streamGenerateContent"
                            else "application/json"
                        ),
                        **provider_headers,
                    },
                    body=sanitized_body,
                )
        except TimeoutError as error:
            await _settle(
                env,
                request_id=request_id,
                uid=str(context["uid"]),
                status="failed",
                usage=None,
                now=int(time.time()),
                cost_micros=None,
                error="provider_timeout",
            )
            reserved = False
            raise GeminiProviderError(
                "gemini_provider_timeout",
                "Gemini provider timed out before returning a response",
                status_code=504,
                retryable=False,
            ) from error
        except Exception as error:
            await _settle(
                env,
                request_id=request_id,
                uid=str(context["uid"]),
                status="failed",
                usage=None,
                now=int(time.time()),
                cost_micros=None,
                error="provider_transport_error",
            )
            reserved = False
            raise GeminiProviderError(
                "gemini_provider_unavailable",
                "Gemini provider is unavailable",
                status_code=502,
                retryable=True,
            ) from error
    except GeminiProviderError as error:
        return _json_error(
            request_id,
            error.code,
            error.message,
            status_code=error.status_code,
            retryable=error.retryable,
            retry_after=getattr(error, "retry_after", None),
            provider=provider,
        )
    except Exception:
        if reserved:
            await _settle(
                env,
                request_id=request_id,
                uid=str(context["uid"]),
                status="failed",
                usage=None,
                now=int(time.time()),
                cost_micros=None,
                error="provider_unavailable",
            )
        return _json_error(
            request_id,
            "gemini_provider_unavailable",
            "Gemini provider is unavailable",
            status_code=502,
            retryable=True,
            provider=provider,
        )

    status = int(getattr(response, "status", 502))
    headers = {
        "cache-control": "no-store",
        "x-request-id": request_id,
        "x-omi-request-id": request_id,
        "x-omi-provider": provider,
    }
    streaming = stream_route or action == "streamGenerateContent"
    if status >= 400:
        proxy_status, code, message, retryable, retry_after = _provider_error(status)
        await _settle(
            env,
            request_id=request_id,
            uid=str(context["uid"]),
            status="failed",
            usage=None,
            now=int(time.time()),
            cost_micros=None,
            error=code,
        )
        return _json_error(
            request_id,
            code,
            message,
            status_code=proxy_status,
            retryable=retryable,
            retry_after=retry_after,
            provider=provider,
        )
    if streaming:
        headers["content-type"] = _response_header(response, "content-type", "text/event-stream")
        return StreamingResponse(
            _stream_body(response, env=env, uid=str(context["uid"]), request_id=request_id),
            status_code=status,
            headers=headers,
            media_type=None,
        )
    response_body = await _array_buffer(response)
    if len(response_body) > MAX_RESPONSE_BYTES:
        await _settle(
            env,
            request_id=request_id,
            uid=str(context["uid"]),
            status="failed",
            usage=None,
            now=int(time.time()),
            cost_micros=None,
            error="response_too_large",
        )
        return _json_error(
            request_id,
            "gemini_response_too_large",
            "Gemini provider response is too large",
            status_code=502,
            retryable=False,
            provider=provider,
        )
    try:
        payload = json.loads(response_body)
    except (TypeError, ValueError):
        payload = None
    if provider == "vertex_ai" and action == "embedContent":
        response_body = _vertex_embedding_response(response_body)
        try:
            payload = json.loads(response_body)
        except (TypeError, ValueError):
            payload = None
    usage = _usage(payload) if isinstance(payload, dict) else None
    if not await _settle(
        env,
        request_id=request_id,
        uid=str(context["uid"]),
        status="success",
        usage=usage,
        now=int(time.time()),
        cost_micros=_cost_micros(env, usage),
    ):
        return _json_error(
            request_id,
            "gemini_usage_unavailable",
            "Gemini usage ledger is unavailable",
            status_code=503,
            retryable=True,
            provider=provider,
        )
    headers["content-type"] = _response_header(response, "content-type", "application/json")
    return Response(content=response_body, status_code=status, headers=headers)


@router.post("/v1/proxy/gemini")
@router.post("/v1/proxy/gemini/{path:path}")
async def gemini_proxy(request: Request, path: str = "") -> Response:
    return await _proxy(request, path, stream_route=False)


@router.post("/v1/proxy/gemini-stream")
@router.post("/v1/proxy/gemini-stream/{path:path}")
async def gemini_stream_proxy(request: Request, path: str = "") -> Response:
    return await _proxy(request, path, stream_route=True)


__all__ = ["gemini_proxy", "gemini_stream_proxy", "router"]
