"""Owner-scoped MCP tool projections for the explicit Cloudflare app seam.

The external-MCP OAuth worker owns provider credentials and discovery writes.
This read-only projection is deliberately separate from the legacy app routes:
it exposes only an installed user's last *ready* tool definitions and never
returns provider endpoints, OAuth metadata, credentials, or execution state.
Provider calls and tool execution must remain behind a future, separately
authenticated runtime boundary.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()

MAX_APPS = 100
MAX_APP_ID_LENGTH = 256
MAX_APP_PAYLOAD_BYTES = 500_000
MAX_TOOL_COUNT = 256
MAX_TOOL_NAME_BYTES = 256
MAX_TOOL_DESCRIPTION_BYTES = 8_192
MAX_TOOL_SCHEMA_BYTES = 512_000
MAX_TOOL_SCHEMA_DEPTH = 16
MAX_TOOL_SCHEMA_PROPERTIES = 256
MAX_TOOL_PROJECTION_BYTES = 2_000_000
CONTROL_CHARACTERS = re.compile(r"[\u0000-\u001f\u007f]")


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _schema_is_bounded(value: object, *, depth: int = 0, properties: int = 0) -> int:
    if depth > MAX_TOOL_SCHEMA_DEPTH:
        raise ValueError("tool schema too deep")
    if value is None or isinstance(value, (str, int, float, bool)):
        return properties
    if isinstance(value, list):
        if len(value) > MAX_TOOL_SCHEMA_PROPERTIES:
            raise ValueError("tool schema too large")
        total = properties
        for item in value:
            total = _schema_is_bounded(item, depth=depth + 1, properties=total)
        return total
    if not isinstance(value, dict):
        raise ValueError("invalid tool schema")
    if len(value) > MAX_TOOL_SCHEMA_PROPERTIES:
        raise ValueError("tool schema too large")
    total = properties + len(value)
    if total > MAX_TOOL_SCHEMA_PROPERTIES:
        raise ValueError("tool schema too large")
    for item in value.values():
        total = _schema_is_bounded(item, depth=depth + 1, properties=total)
    return total


def _tool_projection(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid tool")
    name = value.get("name")
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name.encode("utf-8")) > MAX_TOOL_NAME_BYTES
        or CONTROL_CHARACTERS.search(name)
    ):
        raise ValueError("invalid tool name")
    result: dict[str, object] = {"name": name.strip()}
    description = value.get("description")
    if description is not None:
        if not isinstance(description, str) or len(description.encode("utf-8")) > MAX_TOOL_DESCRIPTION_BYTES:
            raise ValueError("invalid tool description")
        result["description"] = description
    schema = value.get("inputSchema")
    if schema is not None:
        if not isinstance(schema, dict):
            raise ValueError("invalid tool schema")
        schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(schema_json.encode("utf-8")) > MAX_TOOL_SCHEMA_BYTES:
            raise ValueError("tool schema too large")
        _schema_is_bounded(schema)
        result["inputSchema"] = schema
    return result


def _app_metadata(raw: object, app_id: str) -> dict[str, object]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_APP_PAYLOAD_BYTES:
        raise ValueError("invalid app payload")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("id") not in (None, app_id):
        raise ValueError("invalid app payload")
    result: dict[str, object] = {"app_id": app_id}
    for key, limit in (("name", 256), ("description", 8_192)):
        value = payload.get(key)
        if value is not None:
            if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
                raise ValueError("invalid app payload")
            result[key] = value
    return result


def _app_id(request: Request) -> str | None:
    value = request.query_params.get("app_id")
    if value is None:
        return None
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if value and len(value) <= MAX_APP_ID_LENGTH else ""


@router.get("/v2/cf/apps/mcp/tools")
async def get_mcp_app_tools(request: Request):
    """Return installed, ready MCP tool definitions for the current user.

    A failed discovery is intentionally omitted even when a stale tools JSON
    remains in D1. This prevents a caller from treating a known provider
    failure as an executable current tool list.
    """

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    requested_app_id = _app_id(request)
    if requested_app_id == "":
        return JSONResponse({"error": "invalid app_id"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    where = (
        "u.uid = ? AND c.owner_uid = ? AND d.owner_uid = ? AND c.status = 'authorized' "
        "AND d.status = 'ready' AND a.disabled = 0"
    )
    arguments: list[object] = [uid, uid, uid]
    if requested_app_id is not None:
        where += " AND u.app_id = ?"
        arguments.append(requested_app_id)
    try:
        result = await env.APP_DB.prepare(
            "SELECT u.app_id, a.data_json, d.protocol_version, d.revision, d.tools_json "
            "FROM cf_user_enabled_apps u "
            "JOIN cf_app_catalog a ON a.id = u.app_id "
            "JOIN cf_mcp_app_connections c ON c.app_id = u.app_id "
            "JOIN cf_mcp_app_discoveries d ON d.app_id = u.app_id "
            f"WHERE {where} ORDER BY u.created_at ASC, u.app_id ASC LIMIT ?"
        ).bind(*arguments, MAX_APPS).all()
    except Exception:
        return JSONResponse({"error": "mcp tools unavailable"}, status_code=503)

    rows = result.get("results", []) if isinstance(result, dict) else []
    if not isinstance(rows, list):
        return JSONResponse({"error": "mcp tools unavailable"}, status_code=503)
    apps: list[dict[str, object]] = []
    try:
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("app_id"), str):
                raise ValueError("invalid mcp app row")
            app_id = str(row["app_id"])
            metadata = _app_metadata(row.get("data_json"), app_id)
            protocol = row.get("protocol_version")
            revision = row.get("revision")
            if not isinstance(protocol, str) or not protocol or len(protocol) > 64:
                raise ValueError("invalid protocol version")
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
                raise ValueError("invalid discovery revision")
            raw_tools = row.get("tools_json")
            if not isinstance(raw_tools, str) or len(raw_tools.encode("utf-8")) > MAX_TOOL_PROJECTION_BYTES:
                raise ValueError("invalid tools projection")
            tools = json.loads(raw_tools)
            if not isinstance(tools, list) or not tools or len(tools) > MAX_TOOL_COUNT:
                raise ValueError("invalid tools projection")
            projected = [_tool_projection(tool) for tool in tools]
            names = [str(tool["name"]) for tool in projected]
            if len(names) != len(set(names)):
                raise ValueError("duplicate tool name")
            metadata.update({"protocol_version": protocol, "revision": revision, "tools": projected})
            apps.append(metadata)
    except (TypeError, ValueError, json.JSONDecodeError, OverflowError):
        return JSONResponse({"error": "mcp tools unavailable"}, status_code=503)
    response: dict[str, object] = {"apps": apps, "count": len(apps)}
    try:
        if len(json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > MAX_TOOL_PROJECTION_BYTES:
            return JSONResponse({"error": "mcp tools unavailable"}, status_code=503)
    except (TypeError, ValueError, OverflowError):
        return JSONResponse({"error": "mcp tools unavailable"}, status_code=503)
    return JSONResponse(response, headers={"cache-control": "no-store"})
