"""Workers AI-backed persona greeting reads."""

from __future__ import annotations

import json
import hashlib
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fallback import record_fallback
from internal_auth import create_request_context, decode_context

router = APIRouter()

MAX_USERNAME_LENGTH = 256
MAX_PROMPT_BYTES = 500_000
MAX_MESSAGE_LENGTH = 512
MAX_PERSONA_NAME_LENGTH = 120
MAX_PERSONA_USERNAME_LENGTH = 120
MAX_PERSONA_CONTEXT_CHARS = 20_000
DEFAULT_WORKERS_AI_MODEL = "@cf/meta/llama-3.2-3b-instruct"


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _response_text(result: object) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("response", "text", "content"):
            if key in result:
                return _response_text(result[key])
        return ""
    if isinstance(result, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text") or item.get("content") or "")
            for item in result
            if isinstance(item, (str, dict))
        )
    return ""


def _persona(payload: object) -> tuple[str, str] | None:
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_PROMPT_BYTES:
        return None
    try:
        value = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    capabilities = value.get("capabilities")
    name = value.get("name")
    prompt = value.get("persona_prompt")
    if (
        not isinstance(capabilities, list)
        or "persona" not in capabilities
        or not isinstance(name, str)
        or not name.strip()
        or not isinstance(prompt, str)
        or not prompt.strip()
    ):
        return None
    return name.strip()[:128], prompt.strip()


def _persona_from_row(row: object, uid: str) -> dict[str, object] | None:
    """Decode one owner-scoped persona projection without exposing other apps."""

    if not isinstance(row, dict) or row.get("owner_uid") != uid:
        return None
    raw = row.get("data_json")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("invalid persona payload")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("invalid persona payload")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list) or "persona" not in capabilities:
        return None
    result = dict(payload)
    result["id"] = str(row.get("id") or result.get("id") or "")
    result["uid"] = uid
    result["approved"] = bool(row.get("approved"))
    result["rejected"] = not result["approved"]
    result["status"] = str(row.get("status") or result.get("status") or "under-review")
    result["disabled"] = bool(row.get("disabled"))
    return result


async def _existing_user_persona(request: Request, uid: str) -> dict[str, object] | None:
    result = (
        await request.scope["env"]
        .APP_DB.prepare(
            "SELECT id, owner_uid, approved, status, disabled, data_json "
            "FROM cf_app_catalog WHERE owner_uid = ? ORDER BY updated_at DESC, id DESC LIMIT 20"
        )
        .bind(uid)
        .all()
    )
    rows = result.get("results", []) if isinstance(result, dict) else []
    for row in rows:
        persona = _persona_from_row(row, uid)
        if persona is not None:
            return persona
    return None


async def _auth_profile(request: Request, uid: str) -> dict[str, str | None]:
    """Read the Better Auth profile through the signed internal service boundary."""

    env = request.scope["env"]
    auth = getattr(env, "AUTH", None)
    signed = create_request_context(
        uid,
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
        audience="auth",
        method="GET",
        path="/internal/profile",
        request_id=(request.headers.get("x-request-id") or "persona-profile")[:128],
    )
    if auth is None or signed is None:
        return {"name": None, "email": None}
    encoded, signature = signed
    try:
        response = await auth.fetch(
            "https://auth.internal/internal/profile",
            method="GET",
            headers={
                "x-omi-auth-context": encoded,
                "x-omi-internal-signature": signature,
                "x-request-id": (request.headers.get("x-request-id") or "persona-profile")[:128],
            },
        )
        if int(getattr(response, "status", 0)) != 200:
            return {"name": None, "email": None}
        payload = await response.json()
    except Exception:
        return {"name": None, "email": None}
    if not isinstance(payload, dict) or payload.get("uid") != uid:
        return {"name": None, "email": None}
    name = payload.get("name")
    email = payload.get("email")
    return {
        "name": name.strip()[:MAX_PERSONA_NAME_LENGTH] if isinstance(name, str) and name.strip() else None,
        "email": email.strip()[:512] if isinstance(email, str) and email.strip() else None,
    }


def _slug(value: str) -> str:
    compact = "".join(char for char in value.lower() if char.isalnum())
    return (compact or "mypersona")[:MAX_PERSONA_USERNAME_LENGTH]


async def _unique_username(request: Request, base: str, uid: str) -> str:
    """Preserve the legacy username shape while making retries deterministic."""

    env = request.scope["env"]
    candidate = _slug(base)
    try:
        rows = (
            await env.APP_DB.prepare(
                "SELECT data_json FROM cf_app_catalog WHERE json_extract(data_json, '$.username') = ? LIMIT 1"
            )
            .bind(candidate)
            .all()
        )
    except Exception:
        rows = {"results": []}
    if not isinstance(rows, dict) or not rows.get("results"):
        return candidate
    suffix = hashlib.sha256(f"{uid}\x1f{base}".encode("utf-8")).hexdigest()[:8]
    return f"{candidate[:MAX_PERSONA_USERNAME_LENGTH - 9]}_{suffix}"


async def _persona_prompt(request: Request, uid: str, name: str) -> str:
    """Build a bounded clone prompt from the Cloudflare-owned projections."""

    env = request.scope["env"]
    facts: list[str] = []
    conversations: list[str] = []
    try:
        memory_rows = (
            await env.APP_DB.prepare(
                "SELECT content FROM cf_memories WHERE uid = ? AND deleted_at IS NULL AND invalid_at IS NULL "
                "AND memory_tier != 'archive' AND COALESCE(user_review, 1) != 0 AND is_locked = 0 "
                "ORDER BY updated_at DESC, id DESC LIMIT 250"
            )
            .bind(uid)
            .all()
        )
        for row in (memory_rows.get("results", []) if isinstance(memory_rows, dict) else []):
            content = row.get("content") if isinstance(row, dict) else None
            if isinstance(content, str) and content.strip():
                facts.append(" ".join(content.split())[:500])
        conversation_rows = (
            await env.APP_DB.prepare(
                "SELECT structured_json, transcript_segments_json FROM cf_conversations "
                "WHERE uid = ? AND discarded = 0 AND is_locked = 0 AND status = 'completed' "
                "ORDER BY created_at DESC, id DESC LIMIT 10"
            )
            .bind(uid)
            .all()
        )
        for row in (conversation_rows.get("results", []) if isinstance(conversation_rows, dict) else []):
            if not isinstance(row, dict):
                continue
            try:
                structured = json.loads(str(row.get("structured_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                structured = {}
            title = structured.get("title") if isinstance(structured, dict) else None
            overview = structured.get("overview") if isinstance(structured, dict) else None
            parts = [str(value).strip() for value in (title, overview) if isinstance(value, str) and value.strip()]
            if parts:
                conversations.append(" — ".join(parts)[:800])
    except Exception:
        # Prompt context is enrichment, not authorization. Keep the stable base prompt if a projection is unavailable.
        facts = []
        conversations = []

    facts_text = "\n".join(f"- {item}" for item in facts)
    conversation_text = "\n".join(f"- {item}" for item in conversations)
    prompt = f"""
    You are {name} AI. Your objective is to personify {name} as accurately as possible for 1:1 cloning.

    You have:
    - **Condensed Facts:** Core identity, personality, values, behavioral patterns, preferences, and continuity for ongoing projects or relationships.
    - **Condensed Context from Recent Conversations:** Key themes, communication nuances, emotional undertones, decision-making patterns, and conversational flow.

    **Style:**
    - Highly interactive and opinionated.
    - Strong, slightly polarizing opinions based on {name}'s identity.
    - Reference personal facts naturally.
    - 2-3 sentence responses, never more than 20 words.
    - Maintain conversational flow with coherent, context-aware replies.

    **Guidelines:**
    1. Seamlessly integrate facts and recent contexts for continuity, recency, and relevance.
    2. Use the condensed facts as foundational knowledge and ongoing continuity.
    3. Leverage recent conversations to maintain situational awareness and contextual accuracy.
    4. Emulate {name}'s humor, emotional responses, vocabulary, and tone.
    5. Respond with the appropriate level of detail and complexity as {name} would.
    6. Exhibit the same thought processes, decision-making patterns, and conversational dynamics.

    **Rules:**
    - Never mention being AI.
    - No ** or formal language.
    - No unnecessary text or explanations.
    - Maintain the illusion of continuity and authenticity at all times.
    - Every response must be indistinguishable from how {name} would naturally respond.

    Personal Facts and Context:
    {facts_text or 'No imported facts are available yet.'}

    Recent Conversations:
    {conversation_text or 'No recent conversations are available yet.'}

    Use these facts and conversations to shape your personality. Responses should help the conversation go deeper. You have strong opinions, slightly controversial.
    """
    return prompt.strip()[:MAX_PERSONA_CONTEXT_CHARS]


@router.post("/v1/user/persona")
async def get_or_create_user_persona(request: Request):
    """Return or create the user's default Persona in the Cloudflare D1 catalog."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    try:
        existing = await _existing_user_persona(request, uid)
        if existing is not None:
            return existing
        profile = await _auth_profile(request, uid)
        name = profile["name"] or str(context.get("displayName") or "My Persona").strip()[:MAX_PERSONA_NAME_LENGTH]
        if not name:
            name = "My Persona"
        username = await _unique_username(request, name, uid)
        persona_id = "cf_persona_" + hashlib.sha256(uid.encode("utf-8")).hexdigest()[:32]
        now = int(time.time())
        payload = {
            "id": persona_id,
            "name": name,
            "username": username,
            "description": f"This is {name}'s personal AI clone.",
            "image": "",
            "uid": uid,
            "author": name,
            "email": profile["email"] or "",
            "approved": False,
            "status": "under-review",
            "category": "personality-emulation",
            "capabilities": ["persona"],
            "connected_accounts": ["omi"],
            "private": True,
            "persona_prompt": await _persona_prompt(request, uid, name),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        await request.scope["env"].APP_DB.prepare(
            "INSERT INTO cf_app_catalog "
            "(id, approved, status, disabled, is_popular, installs, rating_avg, rating_count, data_json, updated_at, owner_uid) "
            "VALUES (?, 0, 'under-review', 0, 0, 0, NULL, 0, ?, ?, ?) ON CONFLICT(id) DO NOTHING"
        ).bind(persona_id, encoded, now, uid).run()
        created = await _existing_user_persona(request, uid)
        if created is not None:
            return created
        return JSONResponse({"error": "persona unavailable"}, status_code=503)
    except Exception:
        return JSONResponse({"error": "persona unavailable"}, status_code=503)


@router.get("/v1/personas/twitter/initial-message")
async def get_persona_initial_message(request: Request, username: str):
    if not _auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    username = username.removeprefix("@").strip()
    if not username or len(username) > MAX_USERNAME_LENGTH:
        return JSONResponse({"error": "invalid username"}, status_code=400)

    try:
        result = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT data_json FROM cf_app_catalog "
                "WHERE json_extract(data_json, '$.username') = ? AND disabled = 0 LIMIT 1"
            )
            .bind(username)
            .first()
        )
    except Exception:
        return JSONResponse({"error": "persona unavailable"}, status_code=503)
    persona = _persona(result.get("data_json")) if isinstance(result, dict) else None
    if persona is None:
        return {"message": ""}

    name, system_prompt = persona
    ai = getattr(request.scope["env"], "AI", None)
    if ai is None:
        record_fallback(
            component="llm",
            from_mode="workers_ai",
            to_mode="none",
            reason="dependency_unavailable",
            outcome="exhausted",
        )
        return JSONResponse({"error": "persona unavailable"}, status_code=503)
    try:
        result = await ai.run(
            getattr(request.scope["env"], "WORKERS_AI_INTEGRATION_MODEL", DEFAULT_WORKERS_AI_MODEL),
            {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": (
                            f"Generate a short, funny 5-8 word message that would make someone want to chat with you. "
                            f"Be casual and witty, but don't mention being AI or a clone. Just be {name}. "
                            "The message should feel natural and make people curious to chat with you."
                        ),
                    },
                ],
                "max_tokens": 64,
                "temperature": 0.7,
            },
        )
    except Exception:
        record_fallback(
            component="llm",
            from_mode="workers_ai",
            to_mode="none",
            reason="dependency_unavailable",
            outcome="exhausted",
        )
        return JSONResponse({"error": "persona unavailable"}, status_code=503)

    message = re.sub(r"\s+", " ", _response_text(result)).strip().strip('"')[:MAX_MESSAGE_LENGTH]
    if not message:
        record_fallback(
            component="llm",
            from_mode="workers_ai",
            to_mode="none",
            reason="malformed_doc",
            outcome="exhausted",
        )
        return JSONResponse({"error": "persona unavailable"}, status_code=503)
    return {"message": message}


__all__ = ["get_persona_initial_message", "router"]
