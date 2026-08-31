"""Cloudflare-native agent tool directory.

The legacy ``/v1/agent/tools`` handler builds a per-user LangChain tool set
from Firestore-backed app installations.  Cloudflare does not import that
runtime, so this route advertises only the first-party tools whose backing
operations are already owned by API Core/D1 (conversation, memory, and task
read/search/mutation routes).  Third-party app tools are deliberately omitted
until their Worker adapter is implemented; a partial directory is safer than
claiming an execution capability that would fail after tool selection.
"""

from __future__ import annotations

from copy import deepcopy

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


# Keep this list stable: model prompt caching and released clients both depend
# on deterministic tool names and JSON-schema shapes.  ``config`` is an
# internal LangChain parameter and must never be exposed to callers.
CLOUDFLARE_TOOL_DEFINITIONS: tuple[dict[str, object], ...] = (
    {
        "name": "get_conversations_tool",
        "description": "Retrieve the user's recent conversations.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "include_transcript": {"type": "boolean", "default": True},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "search_conversations_tool",
        "description": "Search the user's conversations by meaning or text.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                "include_transcript": {"type": "boolean", "default": True},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_memories_tool",
        "description": "Retrieve the user's memories.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 50},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "search_memories_tool",
        "description": "Search the user's memories by meaning or text.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_action_items_tool",
        "description": "Retrieve the user's action items and to-dos.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 50},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "completed": {"type": "boolean"},
                "conversation_id": {"type": "string"},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "due_start_date": {"type": "string"},
                "due_end_date": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "create_action_item_tool",
        "description": "Create a new action item or to-do for the user.",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "minLength": 1, "maxLength": 4096},
                "due_at": {"type": "string"},
                "conversation_id": {"type": "string"},
            },
            "required": ["description"],
        },
    },
    {
        "name": "update_action_item_tool",
        "description": "Update an action item's completion, description, or due date.",
        "parameters": {
            "type": "object",
            "properties": {
                "action_item_id": {"type": "string", "minLength": 1},
                "completed": {"type": "boolean"},
                "description": {"type": "string", "maxLength": 4096},
                "due_at": {"type": "string"},
            },
            "required": ["action_item_id"],
        },
    },
)


@router.get("/v1/agent/tools")
async def list_tools(request: Request):
    """Return the authenticated user's Cloudflare-native tool directory."""

    if not _auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # Deep-copy nested schemas so a caller cannot mutate the module-level cache.
    return {"tools": deepcopy(CLOUDFLARE_TOOL_DEFINITIONS)}


__all__ = ["router", "list_tools", "CLOUDFLARE_TOOL_DEFINITIONS"]
