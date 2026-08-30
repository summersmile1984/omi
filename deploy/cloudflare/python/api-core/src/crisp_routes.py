"""Crisp support-message reads for the Cloudflare API Core Worker.

The legacy endpoint looks up the caller's support conversation and returns
operator messages newer than ``since``.  The Worker keeps that contract while
using Better Auth for the non-sensitive profile lookup and the external Crisp
HTTP API for the conversation data.  No Crisp credentials or message content
is persisted in Cloudflare storage.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from fallback import record_fallback
from internal_auth import create_request_context, decode_context

try:
    from workers import fetch as worker_fetch
except ModuleNotFoundError as error:  # CPython unit tests do not provide Pyodide's js module.
    if error.name != "js":
        raise
    worker_fetch = None  # type: ignore[assignment]


router = APIRouter()

MAX_PROFILE_EMAIL_LENGTH = 512
MAX_MESSAGES = 1_000


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _empty_response() -> dict[str, object]:
    return {"unread_count": 0, "messages": []}


def _record_empty_fallback(reason: str) -> dict[str, object]:
    # Crisp is an optional support integration.  Keep the released empty
    # response when its config/profile/session is unavailable and emit only a
    # bounded, PII-free fallback event.
    record_fallback(
        component="other",
        from_mode="none",
        to_mode="none",
        reason=reason,
        outcome="degraded",
    )
    return _empty_response()


def _crisp_headers(identifier: str, key: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{identifier}:{key}".encode("utf-8")).decode("ascii")
    return {
        "authorization": f"Basic {encoded}",
        "x-crisp-tier": "plugin",
        "accept": "application/json",
        "user-agent": "omi-cloudflare-worker/0.1",
    }


def _path_component(value: str) -> str:
    return quote(value, safe="")


async def _fetch_json(url: str, headers: dict[str, str]) -> tuple[int, object]:
    if worker_fetch is None:
        raise RuntimeError("Workers fetch is unavailable")
    response = await worker_fetch(url, method="GET", headers=headers)
    status = int(getattr(response, "status", 0))
    try:
        payload = await response.json()
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Crisp returned malformed JSON") from error
    return status, payload


async def _profile_email(request: Request, uid: str) -> str | None:
    env = request.scope["env"]
    auth = getattr(env, "AUTH", None)
    signed = create_request_context(
        uid,
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
        audience="auth",
        method="GET",
        path="/internal/profile",
        request_id=(request.headers.get("x-request-id") or "crisp-profile")[:128],
    )
    if auth is None or signed is None:
        return None
    encoded, signature = signed
    response = await auth.fetch(
        "https://auth.internal/internal/profile",
        method="GET",
        headers={
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
            "x-request-id": (request.headers.get("x-request-id") or "crisp-profile")[:128],
        },
    )
    if int(getattr(response, "status", 0)) != 200:
        return None
    payload = await response.json()
    if not isinstance(payload, dict) or payload.get("uid") != uid:
        return None
    email = payload.get("email")
    if not isinstance(email, str):
        return None
    email = email.strip().lower()
    return email if email and len(email) <= MAX_PROFILE_EMAIL_LENGTH else None


async def _find_session(website_id: str, email: str, headers: dict[str, str]) -> str | None:
    base = f"https://api.crisp.chat/v1/website/{_path_component(website_id)}/conversations"
    for page in range(1, 6):
        status, payload = await _fetch_json(f"{base}/{page}", headers)
        if status < 200 or status >= 300:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            return None
        for conversation in data:
            if not isinstance(conversation, dict):
                continue
            meta = conversation.get("meta")
            candidate = meta.get("email") if isinstance(meta, dict) else None
            session_id = conversation.get("session_id")
            if (
                isinstance(candidate, str)
                and candidate.strip().lower() == email
                and isinstance(session_id, str)
                and session_id
            ):
                return session_id
    return None


def _operator_messages(payload: object, since: int) -> list[dict[str, object]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    messages: list[dict[str, object]] = []
    for message in data:
        if not isinstance(message, dict):
            continue
        timestamp = message.get("timestamp")
        content = message.get("content")
        if (
            message.get("from") != "operator"
            or message.get("type") != "text"
            or not isinstance(timestamp, int)
            or timestamp <= since
            or content is None
        ):
            continue
        text = content if isinstance(content, str) else json.dumps(content, separators=(",", ":"))
        messages.append({"text": text, "timestamp": timestamp, "from": "operator"})
        if len(messages) >= MAX_MESSAGES:
            break
    return messages


@router.get("/v1/crisp/unread")
async def get_crisp_unread(request: Request, since: int = Query(default=0, ge=0)):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    env = request.scope["env"]
    identifier = getattr(env, "CRISP_PLUGIN_IDENTIFIER", None)
    key = getattr(env, "CRISP_PLUGIN_KEY", None)
    website_id = getattr(env, "CRISP_WEBSITE_ID", None)
    if not all(isinstance(value, str) and value.strip() for value in (identifier, key, website_id)):
        return _record_empty_fallback("dependency_unavailable")

    try:
        email = await _profile_email(request, str(context["uid"]))
    except Exception:
        return _record_empty_fallback("dependency_unavailable")
    if not email:
        return _record_empty_fallback("dependency_unavailable")

    headers = _crisp_headers(identifier.strip(), key.strip())
    try:
        session_id = await _find_session(website_id.strip(), email, headers)
        if not session_id:
            return _record_empty_fallback("dependency_unavailable")
        status, payload = await _fetch_json(
            "https://api.crisp.chat/v1/website/"
            f"{_path_component(website_id.strip())}/conversation/{_path_component(session_id)}/messages",
            headers,
        )
        if status < 200 or status >= 300:
            return _record_empty_fallback("dependency_unavailable")
        messages = _operator_messages(payload, since)
    except ValueError:
        return JSONResponse({"error": "Crisp request failed"}, status_code=502)
    except Exception:
        return _record_empty_fallback("dependency_unavailable")
    return {"unread_count": len(messages), "messages": messages}


__all__ = ["router"]
