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
import json
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from internal_auth import decode_context
from tool_routes import (
    create_action_item as _create_action_item,
    get_action_items as _get_action_items,
    get_conversations as _get_conversations,
    get_memories as _get_memories,
    search_conversations as _search_conversations,
    search_memories as _search_memories,
    update_action_item as _update_action_item,
)

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


class ExecuteToolRequest(BaseModel):
    model_config = {"extra": "ignore"}

    tool_name: str = Field(min_length=1, max_length=128)
    params: dict[str, object] = Field(default_factory=dict)


class ExecuteToolResponse(BaseModel):
    result: str | None = None
    error: str | None = None


_QUERY_TOOLS = {
    "get_conversations_tool": _get_conversations,
    "get_memories_tool": _get_memories,
    "get_action_items_tool": _get_action_items,
}
_BODY_TOOLS = {
    "search_conversations_tool": _search_conversations,
    "search_memories_tool": _search_memories,
    "create_action_item_tool": _create_action_item,
}


def _clone_request(request: Request, *, body: bytes = b"", query: dict[str, object] | None = None) -> Request:
    """Build a fresh request for a native tool handler without losing auth scope."""

    scope = dict(request.scope)
    scope.setdefault("type", "http")
    if "headers" not in scope:
        scope["headers"] = [
            (str(name).lower().encode(), str(value).encode()) for name, value in request.headers.items()
        ]
    scope["query_string"] = urlencode(query or {}, doseq=True).encode()
    consumed = False

    async def receive():
        nonlocal consumed
        if consumed:
            return {"type": "http.request", "body": b"", "more_body": False}
        consumed = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _query_values(params: dict[str, object]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            values[key] = "true" if value else "false"
        else:
            values[key] = str(value)
    return values


async def _invoke_native(request: Request, body: ExecuteToolRequest) -> dict[str, object] | JSONResponse:
    tool_name = body.tool_name
    if tool_name in _QUERY_TOOLS:
        native_request = _clone_request(request, query=_query_values(body.params))
        response = await _QUERY_TOOLS[tool_name](native_request)
    elif tool_name in _BODY_TOOLS:
        native_request = _clone_request(
            request,
            body=json.dumps(body.params, ensure_ascii=False, separators=(",", ":")).encode(),
        )
        response = await _BODY_TOOLS[tool_name](native_request)
    elif tool_name == "update_action_item_tool":
        action_item_id = body.params.get("action_item_id")
        if not isinstance(action_item_id, str) or not action_item_id:
            return JSONResponse({"detail": "action_item_id is required"}, status_code=422)
        params = {key: value for key, value in body.params.items() if key != "action_item_id"}
        native_request = _clone_request(
            request,
            body=json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode(),
        )
        response = await _update_action_item(native_request, action_item_id)
    else:
        return JSONResponse({"detail": f"Tool '{tool_name}' not found"}, status_code=404)

    if isinstance(response, JSONResponse):
        if response.status_code >= 400:
            return response
        return {"error": "Tool execution failed"}
    if isinstance(response, dict) and isinstance(response.get("result_text"), str):
        return {"result": response["result_text"]}
    return {"error": "Tool execution failed"}


@router.get("/v1/agent/tools")
async def list_tools(request: Request):
    """Return the authenticated user's Cloudflare-native tool directory."""

    if not _auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # Deep-copy nested schemas so a caller cannot mutate the module-level cache.
    return {"tools": deepcopy(CLOUDFLARE_TOOL_DEFINITIONS)}


@router.post("/v1/agent/execute-tool", response_model=ExecuteToolResponse)
async def execute_tool(request: Request):
    """Execute a first-party tool through its Cloudflare-native D1 handler."""

    if not _auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        raw = await request.body()
        if len(raw) > 64_000:
            return JSONResponse({"detail": "request body is too large"}, status_code=422)
        body = ExecuteToolRequest.model_validate(json.loads(raw or b"{}"))
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    return await _invoke_native(request, body)


__all__ = [
    "router",
    "list_tools",
    "execute_tool",
    "CLOUDFLARE_TOOL_DEFINITIONS",
    "ExecuteToolRequest",
    "ExecuteToolResponse",
]
