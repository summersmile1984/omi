"""D1-backed user data export for the Cloudflare runtime."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fallback import record_fallback
from internal_auth import create_request_context, decode_context

router = APIRouter()

_EXPORT_QUERIES = (
    ("conversations", "cf_conversations", "created_at DESC, id DESC"),
    ("memories", "cf_memories", "created_at DESC, id DESC"),
    ("people", "cf_people", "created_at DESC, id DESC"),
    ("action_items", "cf_action_items", "created_at DESC, id DESC"),
    ("goals", "cf_goals", "created_at DESC, id DESC"),
    ("goal_history", "cf_goal_progress_history", "recorded_at DESC, goal_id DESC, date DESC"),
    ("goal_events", "cf_goal_progress_events", "created_at DESC, event_id DESC"),
    ("workstreams", "cf_workstreams", "updated_at DESC, id DESC"),
    ("workstream_events", "cf_workstream_events", "created_at DESC, event_id DESC"),
    # Keep the legacy export names while reading the D1 projections that own
    # the same user-visible records.
    ("workstream_artifact_refs", "cf_workstream_artifacts", "created_at DESC, artifact_id DESC"),
    ("workstream_continuation_checkpoints", "cf_workstream_checkpoints", "updated_at DESC, checkpoint_id DESC"),
    ("chat_messages", "cf_chat_messages", "created_at DESC, id DESC"),
)

_TASK_DATA_ORDER = (
    "candidates",
    "goals",
    "workstreams",
    "staged_tasks",
    "task_recurrence_inbox",
    "task_feedback",
    "task_outcomes",
    "task_interventions",
    "task_attention_overrides",
    "task_context_snapshots",
    "task_open_loop_snapshots",
    "chat_first_proactive_intents",
    "chat_first_deferrals",
    "goal_events",
    "goal_history",
    "workstream_events",
    "workstream_artifact_refs",
    "workstream_continuation_checkpoints",
)


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _decode_json_columns(row: dict[str, object]) -> dict[str, object]:
    """Convert D1's JSON text columns back to their user-facing values."""
    output: dict[str, object] = {}
    for key, value in row.items():
        if key == "uid":
            continue
        if key.endswith("_json") and isinstance(value, str):
            try:
                output[key[:-5]] = json.loads(value)
                continue
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        output[key] = value
    return output


async def _rows(env: object, table: str, order_by: str, uid: str) -> list[dict[str, object]]:
    result = await env.APP_DB.prepare(f"SELECT * FROM {table} WHERE uid = ? ORDER BY {order_by}").bind(uid).all()
    values = result.get("results", []) if isinstance(result, dict) else []
    return [_decode_json_columns(row) for row in values if isinstance(row, dict)]


async def _profile(request: Request, uid: str) -> dict[str, object]:
    """Fetch non-sensitive profile metadata from Better Auth when available."""
    env = request.scope["env"]
    auth = getattr(env, "AUTH", None)
    request_id = (request.headers.get("x-request-id") or "user-export")[:128]
    signed = create_request_context(
        uid,
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
        audience="auth",
        method="GET",
        path="/internal/profile",
        request_id=request_id,
    )
    if auth is None or signed is None:
        record_fallback(
            component="auth",
            from_mode="auth_worker",
            to_mode="metadata_only",
            reason="dependency_unavailable",
            outcome="degraded",
        )
        return {"uid": uid}
    encoded, signature = signed
    try:
        response = await auth.fetch(
            "https://auth.internal/internal/profile",
            method="GET",
            headers={
                "x-omi-auth-context": encoded,
                "x-omi-internal-signature": signature,
                "x-request-id": request_id,
            },
        )
        if int(response.status) != 200:
            raise ValueError("profile lookup rejected")
        payload = await response.json()
        if not isinstance(payload, dict) or payload.get("uid") != uid:
            raise ValueError("profile lookup returned an invalid identity")
        return {
            "uid": uid,
            "name": payload.get("name") if isinstance(payload.get("name"), str) else None,
            "email": payload.get("email") if isinstance(payload.get("email"), str) else None,
        }
    except Exception:
        record_fallback(
            component="auth",
            from_mode="auth_worker",
            to_mode="metadata_only",
            reason="dependency_unavailable",
            outcome="degraded",
        )
        return {"uid": uid}


@router.get("/v1/users/export")
async def export_user_data(request: Request):
    """Export all user-owned D1 data as a downloadable JSON document."""
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    uid = str(context["uid"])
    try:
        sections: dict[str, list[dict[str, object]]] = {}
        for name, table, order_by in _EXPORT_QUERIES:
            sections[name] = await _rows(env, table, order_by, uid)
        profile = await _profile(request, uid)
    except Exception:
        return JSONResponse({"error": "user export unavailable"}, status_code=503)

    task_data = {name: sections.pop(name, []) for name in _TASK_DATA_ORDER}
    # Keep the legacy top-level shape while explicitly documenting that
    # implementation-only queues, leases, and outboxes are not exported.
    payload = {
        "profile": profile,
        "conversations": sections.pop("conversations", []),
        "memories": sections.pop("memories", []),
        "people": sections.pop("people", []),
        "action_items": sections.pop("action_items", []),
        "task_data": task_data,
        "chat_messages": sections.pop("chat_messages", []),
    }
    payload["exported_at"] = int(time.time())
    return JSONResponse(
        payload,
        headers={"Content-Disposition": 'attachment; filename="omi-export.json"'},
    )


__all__ = ["export_user_data", "router"]
