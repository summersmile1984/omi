"""Workers AI-backed default text chat with D1 history persistence."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from chat_quota import (
    free_quota_detail,
    provider_cost_usd,
    provider_usage,
    reserve_chat_question,
    settle_failed_question,
    settlement_statement,
    trial_paywall_applies,
)
from internal_auth import decode_context

router = APIRouter()

DEFAULT_WORKERS_AI_CHAT_MODEL = "@cf/meta/llama-3.2-3b-instruct"
MAX_CHAT_BODY_BYTES = 64_000
MAX_CHAT_TEXT_CHARS = 16_000
MAX_CHAT_RESPONSE_CHARS = 16_000
MAX_CHAT_FILE_IDS = 20
MAX_CHAT_HISTORY_ROWS = 24
MAX_CHAT_HISTORY_CHARS = 32_000
MAX_STORED_MESSAGE_BYTES = 1_000_000
SYSTEM_PROMPT = (
    "You are Omi, a concise and helpful personal assistant. "
    "Answer in the language used by the user. Do not claim access to memories, "
    "files, apps, tools, or live information that was not supplied in this chat."
)


class SendMessageRequest(BaseModel):
    model_config = {"extra": "ignore"}

    text: str = Field(min_length=1, max_length=MAX_CHAT_TEXT_CHARS)
    file_ids: list[str] | None = Field(default_factory=list, max_length=MAX_CHAT_FILE_IDS)
    context: dict[str, object] | None = None


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


async def _bounded_payload(request: Request) -> SendMessageRequest:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_CHAT_BODY_BYTES:
        raise ValueError("chat request body is too large")
    raw = await request.json()
    if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > MAX_CHAT_BODY_BYTES:
        raise ValueError("chat request body is too large")
    payload = SendMessageRequest.model_validate(raw)
    if not payload.text.strip():
        raise ValueError("chat text is empty")
    return payload


def _requested_app_id(request: Request) -> str | None:
    raw = request.query_params.get("app_id") or request.query_params.get("plugin_id")
    return None if raw in {None, "", "null"} else str(raw)


def _account_created_at(context: dict[str, object]) -> int | None:
    value = context.get("accountCreatedAt")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _request_has_all_byok_keys(request: Request) -> bool:
    return all(
        bool(str(request.headers.get(f"x-byok-{provider}") or "").strip())
        for provider in ("openai", "anthropic", "gemini", "deepgram")
    )


def _rpc_mapping(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return {"response": value}
    to_py = getattr(value, "to_py", None)
    if callable(to_py):
        converted = to_py()
        if isinstance(converted, dict):
            return converted
    response = getattr(value, "response", None)
    response_to_py = getattr(response, "to_py", None)
    if callable(response_to_py):
        response = response_to_py()
    return {"response": response} if isinstance(response, str) else None


def _response_text(value: object) -> str | None:
    mapping = _rpc_mapping(value)
    response = mapping.get("response") if mapping else None
    if not isinstance(response, str):
        return None
    normalized = response.strip()
    if not normalized or len(normalized) > MAX_CHAT_RESPONSE_CHARS:
        return None
    return normalized


def _prompt_message(row: dict[str, object]) -> dict[str, str] | None:
    raw = row.get("message_json")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_STORED_MESSAGE_BYTES:
        return None
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(message, dict):
        return None
    sender = message.get("sender")
    text = message.get("text")
    if sender not in {"human", "ai"} or not isinstance(text, str) or not text or len(text) > MAX_CHAT_TEXT_CHARS:
        return None
    return {"role": "user" if sender == "human" else "assistant", "content": text}


async def _history(env: object, uid: str, session_id: str) -> list[dict[str, str]]:
    result = (
        await env.APP_DB.prepare(
            "SELECT message_json FROM cf_chat_messages WHERE uid = ? AND app_id IS NULL AND "
            "COALESCE(NULLIF(json_extract(message_json, '$.chat_session_id'), ''), "
            "NULLIF(json_extract(message_json, '$.session_id'), '')) = ? "
            "AND COALESCE(json_extract(message_json, '$.reported'), 0) != 1 "
            "ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        .bind(uid, session_id, MAX_CHAT_HISTORY_ROWS)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    selected: list[dict[str, str]] = []
    total_chars = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        message = _prompt_message(row)
        if message is None:
            continue
        length = len(message["content"])
        if total_chars + length > MAX_CHAT_HISTORY_CHARS:
            break
        selected.append(message)
        total_chars += length
    selected.reverse()
    return selected


def _message(
    *,
    message_id: str,
    text: str,
    sender: str,
    created_at: datetime,
    session_id: str,
) -> dict[str, object]:
    return {
        "id": message_id,
        "text": text,
        "created_at": created_at.isoformat(),
        "sender": sender,
        "app_id": None,
        "plugin_id": None,
        "from_external_integration": False,
        "type": "text",
        "memories_id": [],
        "memories": [],
        "reported": False,
        "report_reason": None,
        "files_id": [],
        "files": [],
        "chat_session_id": session_id,
        "session_id": session_id,
        "data_protection_level": None,
        "langsmith_run_id": None,
        "prompt_name": None,
        "prompt_commit": None,
        "rating": None,
        "metadata": None,
        "content_blocks": [],
        "client_message_id": None,
        "message_source": None,
        "journal_revision": None,
        "chart_data": None,
    }


async def _persist_exchange(
    env: object,
    uid: str,
    human_message: dict[str, object],
    ai_message: dict[str, object],
    created_at: int,
    session_id: str,
    settlement: object | None = None,
) -> None:
    session_now = int(time.time())
    statements = [
        env.APP_DB.prepare(
            "INSERT OR IGNORE INTO cf_chat_sessions "
            "(uid, id, title, preview, created_at, updated_at, app_id, message_count, starred) "
            "VALUES (?, ?, 'New Chat', NULL, ?, ?, NULL, 0, 0)"
        ).bind(uid, session_id, session_now, session_now)
    ]
    for ordinal, message in enumerate((human_message, ai_message)):
        statements.append(
            env.APP_DB.prepare(
                "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) " "VALUES (?, ?, NULL, ?, ?)"
            ).bind(
                uid,
                str(message["id"]),
                created_at + ordinal,
                json.dumps(message, separators=(",", ":"), ensure_ascii=False),
            )
        )
    statements.append(
        env.APP_DB.prepare(
            "UPDATE cf_chat_sessions SET updated_at = ?, message_count = message_count + 2, preview = ? "
            "WHERE uid = ? AND id = ?"
        ).bind(int(time.time()), str(ai_message["text"])[:100], uid, session_id)
    )
    if settlement is not None:
        statements.append(settlement)
    await env.APP_DB.batch(statements)


async def _default_session_id(env: object, uid: str) -> str:
    row = (
        await env.APP_DB.prepare(
            "SELECT id FROM cf_chat_sessions WHERE uid = ? AND app_id IS NULL "
            "ORDER BY updated_at DESC, id DESC LIMIT 1"
        )
        .bind(uid)
        .first()
    )
    if isinstance(row, dict) and isinstance(row.get("id"), str):
        return str(row["id"])
    return str(uuid.uuid4())


def _exchange_order_key() -> int:
    # Reserve two adjacent, JS-safe integer slots for the human/AI pair. Using
    # seconds allows rapid sequential exchanges to collide and reorder history.
    return int(time.time() * 1_000_000) * 2


def _sse_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "__CRLF__")


async def _response_stream(text: str, message: dict[str, object]):
    yield f"data: {_sse_text(text)}\n\n"
    response_message = {**message, "ask_for_nps": False}
    encoded = base64.b64encode(
        json.dumps(response_message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    yield f"done: {encoded}\n\n"


async def _done_stream(message: dict[str, object]):
    response_message = {**message, "ask_for_nps": False}
    encoded = base64.b64encode(
        json.dumps(response_message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    yield f"done: {encoded}\n\n"


def _quota_exceeded_text(detail: dict[str, object]) -> str:
    plan = str(detail.get("plan") or "Free")
    limit = detail.get("limit")
    if detail.get("unit") == "cost_usd" and isinstance(limit, (int, float)):
        limit_phrase = f"your ${int(limit)} monthly AI compute budget"
    elif isinstance(limit, (int, float)):
        limit_phrase = f"your {int(limit)} monthly chat question limit"
    else:
        limit_phrase = "your monthly chat limit"
    reset_phrase = ""
    reset_at = detail.get("reset_at")
    if isinstance(reset_at, (int, float)):
        reset = datetime.fromtimestamp(int(reset_at), timezone.utc)
        reset_phrase = f" Your limit resets on {reset.strftime('%B')} {reset.day}."
    return (
        f"You've reached {limit_phrase} on the {plan} plan.{reset_phrase}\n\n"
        "Upgrade your plan to keep chatting, or bring your own API keys in Settings to use Omi free."
    )


@router.post("/v2/messages")
async def chat_messages(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = await _bounded_payload(request)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid chat request"}, status_code=400)

    if _requested_app_id(request) is not None:
        return JSONResponse(
            {"error": "app chat is not migrated", "reason": "app_chat_not_migrated"},
            status_code=409,
        )
    if payload.file_ids:
        return JSONResponse(
            {"error": "chat attachments are not migrated", "reason": "attachments_not_migrated"},
            status_code=409,
        )
    if payload.context is not None:
        return JSONResponse(
            {"error": "page-context chat is not migrated", "reason": "context_not_migrated"},
            status_code=409,
        )

    env = request.scope["env"]
    ai = getattr(env, "AI", None)
    app_db = getattr(env, "APP_DB", None)
    if ai is None:
        return JSONResponse(
            {"error": "workers ai is not configured", "reason": "provider_not_configured"},
            status_code=503,
        )
    if app_db is None:
        return JSONResponse({"error": "chat history is not configured"}, status_code=503)

    uid = str(context["uid"])
    try:
        session_id = await _default_session_id(env, uid)
        history = await _history(env, uid, session_id)
    except Exception:
        return JSONResponse({"error": "chat history unavailable"}, status_code=503)
    human_message_id = str(uuid.uuid4())
    quota_key = f"v2_messages:{human_message_id}"
    platform = request.headers.get("x-app-platform")
    account_created_at = _account_created_at(context)
    has_byok_keys = _request_has_all_byok_keys(request)
    try:
        reserved = await reserve_chat_question(
            env,
            uid=uid,
            idempotency_key=quota_key,
            message_id=human_message_id,
            chat_session_id=session_id,
            platform=platform,
            account_created_at=account_created_at,
            has_byok_keys=has_byok_keys,
        )
    except Exception:
        unavailable = _message(
            message_id=str(uuid.uuid4()),
            text="Usage accounting is temporarily unavailable. Please retry in a moment — your message was not saved.",
            sender="ai",
            created_at=datetime.now(timezone.utc),
            session_id=session_id,
        )
        return StreamingResponse(
            _done_stream(unavailable),
            media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"},
        )
    if not reserved:
        try:
            detail = await free_quota_detail(
                env,
                uid,
                force_exhausted=trial_paywall_applies(
                    env,
                    platform=platform,
                    account_created_at=account_created_at,
                    has_byok_keys=has_byok_keys,
                ),
            )
            now = datetime.now(timezone.utc)
            human_message = _message(
                message_id=human_message_id,
                text=payload.text.strip(),
                sender="human",
                created_at=now,
                session_id=session_id,
            )
            quota_message = _message(
                message_id=str(uuid.uuid4()),
                text=_quota_exceeded_text(detail),
                sender="ai",
                created_at=now + timedelta(microseconds=1),
                session_id=session_id,
            )
            await _persist_exchange(env, uid, human_message, quota_message, _exchange_order_key(), session_id)
        except Exception:
            return JSONResponse({"error": "chat quota unavailable"}, status_code=503)
        return StreamingResponse(
            _done_stream(quota_message),
            media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"},
        )
    prompt = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": payload.text.strip()},
    ]
    model = getattr(env, "WORKERS_AI_CHAT_MODEL", DEFAULT_WORKERS_AI_CHAT_MODEL)
    try:
        result = await ai.run(
            model,
            {
                "messages": prompt,
                "stream": False,
                "max_tokens": 512,
                "temperature": 0.4,
            },
        )
        mapped_result = _rpc_mapping(result)
        answer = _response_text(mapped_result)
    except Exception:
        try:
            await settle_failed_question(env, uid=uid, idempotency_key=quota_key, model=model)
        except Exception:
            pass
        return JSONResponse({"error": "workers ai chat unavailable"}, status_code=502)
    usage = provider_usage(mapped_result)
    if answer is None or usage is None:
        try:
            await settle_failed_question(env, uid=uid, idempotency_key=quota_key, model=model)
        except Exception:
            pass
        return JSONResponse({"error": "workers ai returned an invalid chat response"}, status_code=502)

    now = datetime.now(timezone.utc)
    human_message = _message(
        message_id=human_message_id,
        text=payload.text.strip(),
        sender="human",
        created_at=now,
        session_id=session_id,
    )
    ai_message = _message(
        message_id=str(uuid.uuid4()),
        text=answer,
        sender="ai",
        created_at=now + timedelta(microseconds=1),
        session_id=session_id,
    )
    prompt_tokens, completion_tokens = usage
    cost_usd = provider_cost_usd(env, prompt_tokens, completion_tokens)
    settlement = settlement_statement(
        env,
        uid=uid,
        idempotency_key=quota_key,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
    )
    try:
        await _persist_exchange(
            env,
            uid,
            human_message,
            ai_message,
            _exchange_order_key(),
            session_id,
            settlement,
        )
    except Exception:
        # The provider has already completed. Preserve its cost even if message
        # persistence is temporarily unavailable; an Architect projection stays
        # unavailable if this recovery write also fails instead of undercounting.
        try:
            await settlement.run()
        except Exception:
            pass
        return JSONResponse({"error": "chat history unavailable"}, status_code=503)

    return StreamingResponse(
        _response_stream(answer, ai_message),
        media_type="text/event-stream",
        headers={"cache-control": "no-store", "x-accel-buffering": "no"},
    )


__all__ = ["router"]
