"""Knowledge-graph routes derived from canonical D1 memories and Workers AI."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from fallback import record_fallback
from internal_auth import decode_context

router = APIRouter()

DEFAULT_PAGE_LIMIT = 200
MAX_PAGE_LIMIT = 500
MAX_REQUEST_BYTES = 256_000
MAX_EXTRACT_NODES = 100
MAX_EXTRACT_EDGES = 200
DEFAULT_WORKERS_AI_MODEL = "@cf/meta/llama-3.2-3b-instruct"
GRAPH_NODE_TYPES = frozenset({"person", "place", "organization", "thing", "concept"})
CANONICAL_GRAPH_MUTATION_CONFLICT = (
    "Canonical knowledge graph state is derived from canonical memories and cannot be deleted or rebuilt directly."
)

_GRAPH_MEMORY_WHERE = (
    "uid = ? AND deleted_at IS NULL AND invalid_at IS NULL AND memory_tier = 'long_term' "
    "AND is_locked = 0 AND (user_review IS NULL OR user_review != 0)"
)
_GRAPH_MEMORY_SELECT = (
    "SELECT id, content, predicate, arguments_json, subject_entity_id, object_entity_ids_json, "
    "qualifiers_json, created_at, updated_at FROM cf_memories WHERE " + _GRAPH_MEMORY_WHERE
)


class ExtractKnowledgeGraphRequest(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    text: str = Field(min_length=1, max_length=100_000)
    user_name: str | None = Field(default=None, max_length=256)
    include_existing: bool = False


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _json(value: object, default: object) -> object:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return default
        return decoded
    return value if value is not None else default


def _contract_id(namespace: str, payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{namespace}|{canonical}".encode()).hexdigest()


def _entity_id(label: str) -> str:
    normalized = " ".join(label.split()).casefold()
    if normalized == "user":
        return "user"
    if normalized.startswith("ent_"):
        return normalized
    return "ent_" + _contract_id("canonical-graph-entity", {"label": normalized})[:20]


def _clean_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _value_label(value: object, slot: str) -> str:
    if isinstance(value, dict):
        for key in ("label", "value", "entity_id"):
            label = _clean_text(value.get(key), limit=240)
            if label:
                return label
    if isinstance(value, (str, int, float, bool)):
        label = _clean_text(str(value), limit=240)
        if label:
            return label
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = ""
    return _clean_text(encoded, limit=240) or slot


def _node_type(value: object) -> str:
    return value if isinstance(value, str) and value in GRAPH_NODE_TYPES else "thing"


def _merge_node(nodes: dict[str, dict[str, object]], incoming: dict[str, object]) -> None:
    node_id = str(incoming["id"])
    existing = nodes.get(node_id)
    if existing is None:
        nodes[node_id] = incoming
        return
    aliases = set(item for item in existing.get("aliases", []) if isinstance(item, str))
    aliases.update(item for item in incoming.get("aliases", []) if isinstance(item, str))
    memory_ids = set(item for item in existing.get("memory_ids", []) if isinstance(item, str))
    memory_ids.update(item for item in incoming.get("memory_ids", []) if isinstance(item, str))
    existing["aliases"] = sorted(aliases)
    existing["memory_ids"] = sorted(memory_ids)


def _graph_records(rows: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    nodes: dict[str, dict[str, object]] = {}
    edges: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in rows:
        memory_id = _clean_text(row.get("id"), limit=256)
        subject_id = _clean_text(row.get("subject_entity_id"), limit=256)
        predicate = _clean_text(row.get("predicate"), limit=256)
        arguments = _json(row.get("arguments_json"), {})
        object_ids = _json(row.get("object_entity_ids_json"), [])
        if not memory_id or not subject_id or not predicate or not isinstance(arguments, dict) or not arguments:
            continue
        normalized_object_ids = (
            [item for item in object_ids if isinstance(item, str) and item] if isinstance(object_ids, list) else []
        )
        _merge_node(
            nodes,
            {
                "id": subject_id,
                "label": "You" if subject_id == "user" else subject_id,
                "node_type": "person" if subject_id == "user" else "thing",
                "aliases": [],
                "memory_ids": [memory_id],
            },
        )
        for index, (raw_slot, raw_value) in enumerate(sorted(arguments.items(), key=lambda item: str(item[0]))):
            slot = _clean_text(str(raw_slot), limit=64) or "value"
            label = _value_label(raw_value, slot)
            endpoint = raw_value if isinstance(raw_value, dict) else {}
            explicit_id = _clean_text(endpoint.get("entity_id"), limit=256)
            target_id = explicit_id or (
                normalized_object_ids[index] if index < len(normalized_object_ids) else _entity_id(label)
            )
            if target_id == subject_id:
                continue
            _merge_node(
                nodes,
                {
                    "id": target_id,
                    "label": label,
                    "node_type": _node_type(endpoint.get("node_type")),
                    "aliases": [],
                    "memory_ids": [memory_id],
                },
            )
            edge_label = predicate if len(arguments) == 1 else f"{predicate}:{slot}"
            key = (subject_id, target_id, edge_label)
            existing = edges.get(key)
            if existing is None:
                edges[key] = {
                    "id": "edge_"
                    + _contract_id(
                        "canonical-graph-edge",
                        {"source_id": subject_id, "target_id": target_id, "label": edge_label},
                    )[:24],
                    "source_id": subject_id,
                    "target_id": target_id,
                    "label": edge_label,
                    "memory_ids": [memory_id],
                }
            else:
                existing["memory_ids"] = sorted({*existing["memory_ids"], memory_id})
    return [nodes[key] for key in sorted(nodes)], [edges[key] for key in sorted(edges)]


def _iso(value: object) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        timestamp = 0
    return datetime.fromtimestamp(max(0, timestamp), timezone.utc).isoformat().replace("+00:00", "Z")


def _catalog_nodes(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    catalog: list[dict[str, object]] = []
    for row in rows:
        memory_id = _clean_text(row.get("id"), limit=256)
        if not memory_id:
            continue
        label = _clean_text(row.get("content"), limit=240) or "Untitled canonical memory"
        catalog.append(
            {
                "id": f"memory:{memory_id}",
                "label": label,
                "node_type": "concept",
                "aliases": [],
                "memory_ids": [memory_id],
                "created_at": _iso(row.get("created_at")),
                "updated_at": _iso(row.get("updated_at")),
            }
        )
    return catalog


async def _revision(env: object, uid: str) -> str:
    row = (
        await env.APP_DB.prepare(
            "SELECT COUNT(*) AS row_count, COALESCE(MAX(updated_at), 0) AS max_updated_at, "
            "COALESCE(SUM(updated_at), 0) AS updated_sum, COALESCE(MIN(id), '') AS min_id, "
            "COALESCE(MAX(id), '') AS max_id FROM cf_memories WHERE " + _GRAPH_MEMORY_WHERE
        )
        .bind(uid)
        .first()
    )
    if not isinstance(row, dict):
        raise ValueError("knowledge graph revision unavailable")
    return _contract_id("cloudflare-knowledge-graph-revision", row)


def _cursor_secret(env: object) -> bytes:
    secret = getattr(env, "KNOWLEDGE_GRAPH_CURSOR_SECRET", None) or getattr(env, "INTERNAL_ASSERTION_SECRET", None)
    if not isinstance(secret, str) or not secret:
        raise ValueError("knowledge graph cursor unavailable")
    return secret.encode()


def _encode_cursor(env: object, uid: str, row: dict[str, object], revision: str) -> str:
    payload = json.dumps(
        {"uid": uid, "updated_at": int(row["updated_at"]), "id": str(row["id"]), "revision": revision},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(_cursor_secret(env), encoded.encode(), hashlib.sha256).digest()
    return encoded + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def _decode_cursor(env: object, uid: str, cursor: str, revision: str) -> tuple[int, str]:
    try:
        encoded, raw_signature = cursor.split(".", 1)
        expected = hmac.new(_cursor_secret(env), encoded.encode(), hashlib.sha256).digest()
        signature = base64.urlsafe_b64decode(raw_signature + "=" * (-len(raw_signature) % 4))
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        updated_at = payload["updated_at"]
        memory_id = payload["id"]
        if (
            payload.get("uid") != uid
            or payload.get("revision") != revision
            or isinstance(updated_at, bool)
            or not isinstance(updated_at, int)
            or not isinstance(memory_id, str)
            or not memory_id
        ):
            raise ValueError
        return updated_at, memory_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_or_stale_cursor") from exc


async def _page(
    env: object,
    uid: str,
    *,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict[str, object]], bool, str | None]:
    revision = await _revision(env, uid)
    query = _GRAPH_MEMORY_SELECT
    args: list[object] = [uid]
    if cursor:
        updated_at, memory_id = _decode_cursor(env, uid, cursor, revision)
        query += " AND (updated_at < ? OR (updated_at = ? AND id < ?))"
        args.extend((updated_at, updated_at, memory_id))
    query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    args.append(limit + 1)
    result = await env.APP_DB.prepare(query).bind(*args).all()
    rows = result.get("results", []) if isinstance(result, dict) else []
    typed_rows = [row for row in rows if isinstance(row, dict)]
    if await _revision(env, uid) != revision:
        raise RuntimeError("knowledge graph revision changed during read")
    has_more = len(typed_rows) > limit
    page_rows = typed_rows[:limit]
    next_cursor = _encode_cursor(env, uid, page_rows[-1], revision) if has_more and page_rows else None
    return page_rows, has_more, next_cursor


async def _read_graph(env: object, uid: str, *, limit: int, cursor: str | None = None):
    for attempt in range(2):
        try:
            return await _page(env, uid, limit=limit, cursor=cursor)
        except RuntimeError:
            if attempt or cursor:
                raise
    raise RuntimeError("knowledge graph revision changed during read")


@router.get("/v1/knowledge-graph")
async def get_knowledge_graph(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        rows, has_more, _ = await _read_graph(request.scope["env"], str(context["uid"]), limit=MAX_PAGE_LIMIT)
    except Exception:
        return JSONResponse({"detail": "canonical_graph_unavailable"}, status_code=503)
    nodes, raw_edges = _graph_records(rows)
    bounded_nodes = nodes[:MAX_PAGE_LIMIT]
    node_ids = {str(node["id"]) for node in bounded_nodes}
    edges = [
        edge for edge in raw_edges[:MAX_PAGE_LIMIT] if edge["source_id"] in node_ids and edge["target_id"] in node_ids
    ]
    truncated = has_more or len(nodes) > len(bounded_nodes) or len(raw_edges) > len(edges)
    return {
        "nodes": bounded_nodes,
        "edges": edges,
        "truncated": truncated,
        "node_count": len(bounded_nodes),
        "edge_count": len(edges),
        "node_limit": MAX_PAGE_LIMIT,
        "edge_limit": MAX_PAGE_LIMIT,
    }


@router.get("/v1/knowledge-graph/canonical")
async def get_canonical_knowledge_graph(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        limit = int(request.query_params.get("limit") or DEFAULT_PAGE_LIMIT)
    except (TypeError, ValueError):
        return JSONResponse({"detail": "invalid limit"}, status_code=422)
    if limit < 1 or limit > MAX_PAGE_LIMIT:
        return JSONResponse({"detail": "invalid limit"}, status_code=422)
    cursor = request.query_params.get("cursor")
    try:
        rows, has_more, next_cursor = await _read_graph(
            request.scope["env"], str(context["uid"]), limit=limit, cursor=cursor
        )
    except ValueError:
        return JSONResponse({"detail": "invalid_or_stale_cursor"}, status_code=400)
    except Exception:
        return JSONResponse({"detail": "canonical_graph_unavailable"}, status_code=503)
    nodes, edges = _graph_records(rows)
    return {
        "nodes": nodes,
        "edges": edges,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "catalog_nodes": _catalog_nodes(rows),
    }


async def _has_canonical_state(env: object, uid: str) -> bool:
    row = await env.APP_DB.prepare("SELECT state FROM cf_account_cutover WHERE uid = ?").bind(uid).first()
    if not isinstance(row, dict):
        raise ValueError("canonical graph state unavailable")
    return row.get("state") in {"new", "migrating", "rolled_back_stranded"}


async def _reject_canonical_mutation(request: Request, success_status: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        established = await _has_canonical_state(request.scope["env"], str(context["uid"]))
    except Exception:
        return JSONResponse(
            {
                "detail": "Knowledge graph state could not be verified right now, so it was left untouched. Please try again."
            },
            status_code=503,
        )
    if established:
        return JSONResponse({"detail": CANONICAL_GRAPH_MUTATION_CONFLICT}, status_code=409)
    if success_status == "rebuilding":
        return {"status": "rebuilding", "nodes_count": 0, "edges_count": 0}
    return {"status": "deleted"}


@router.post("/v1/knowledge-graph/rebuild")
async def rebuild_knowledge_graph(request: Request):
    return await _reject_canonical_mutation(request, "rebuilding")


@router.delete("/v1/knowledge-graph")
async def delete_knowledge_graph(request: Request):
    return await _reject_canonical_mutation(request, "deleted")


def _structured_json(value: str) -> object | None:
    value = value.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _rpc_mapping(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    to_py = getattr(value, "to_py", None)
    if callable(to_py):
        converted = to_py()
        if isinstance(converted, dict):
            return converted
    return None


def _extract_schema() -> dict[str, object]:
    node = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "label": {"type": "string"},
            "node_type": {"type": "string", "enum": sorted(GRAPH_NODE_TYPES)},
            "aliases": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id", "label", "node_type", "aliases"],
        "additionalProperties": False,
    }
    edge = {
        "type": "object",
        "properties": {
            "source_id": {"type": "string"},
            "target_id": {"type": "string"},
            "label": {"type": "string"},
        },
        "required": ["source_id", "target_id", "label"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "omi_knowledge_graph",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "nodes": {"type": "array", "maxItems": MAX_EXTRACT_NODES, "items": node},
                    "edges": {"type": "array", "maxItems": MAX_EXTRACT_EDGES, "items": edge},
                },
                "required": ["nodes", "edges"],
                "additionalProperties": False,
            },
        },
    }


def _normalized_extraction(value: object) -> dict[str, list[dict[str, object]]] | None:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("nodes"), list)
        or not isinstance(value.get("edges"), list)
    ):
        return None
    nodes: dict[str, dict[str, object]] = {}
    for raw in value["nodes"][:MAX_EXTRACT_NODES]:
        if not isinstance(raw, dict):
            return None
        label = _clean_text(raw.get("label"), limit=240)
        node_id = _clean_text(raw.get("id"), limit=256) or (_entity_id(label) if label else "")
        if not label or not node_id:
            return None
        aliases = raw.get("aliases")
        if not isinstance(aliases, list):
            return None
        nodes[node_id] = {
            "id": node_id,
            "label": label,
            "node_type": _node_type(raw.get("node_type")),
            "aliases": sorted({_clean_text(item, limit=240) for item in aliases if _clean_text(item, limit=240)}),
            "memory_ids": [],
        }
    edges: list[dict[str, object]] = []
    for raw in value["edges"][:MAX_EXTRACT_EDGES]:
        if not isinstance(raw, dict):
            return None
        source_id = _clean_text(raw.get("source_id"), limit=256)
        target_id = _clean_text(raw.get("target_id"), limit=256)
        label = _clean_text(raw.get("label"), limit=256)
        if not source_id or not target_id or not label or source_id == target_id:
            return None
        if source_id not in nodes or target_id not in nodes:
            return None
        edges.append(
            {
                "id": "edge_"
                + _contract_id(
                    "canonical-graph-edge",
                    {"source_id": source_id, "target_id": target_id, "label": label},
                )[:24],
                "source_id": source_id,
                "target_id": target_id,
                "label": label,
                "memory_ids": [],
            }
        )
    return {"nodes": [nodes[key] for key in sorted(nodes)], "edges": edges}


@router.post("/v1/knowledge-graph/extract")
async def extract_knowledge_graph(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        return JSONResponse({"detail": "invalid knowledge graph extraction"}, status_code=400)
    try:
        body = ExtractKnowledgeGraphRequest.model_validate_json(raw)
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"detail": "invalid knowledge graph extraction"}, status_code=400)
    env = request.scope["env"]
    existing = ""
    if body.include_existing:
        try:
            rows, _, _ = await _read_graph(env, str(context["uid"]), limit=100)
        except Exception:
            return JSONResponse({"detail": "canonical_graph_unavailable"}, status_code=503)
        nodes, edges = _graph_records(rows)
        existing = "\nExisting graph (reuse matching IDs): " + json.dumps(
            {"nodes": nodes[:100], "edges": edges[:200]}, ensure_ascii=False, separators=(",", ":")
        )
    ai = getattr(env, "AI", None)
    if ai is None:
        record_fallback(
            component="llm",
            from_mode="workers_ai",
            to_mode="none",
            reason="dependency_unavailable",
            outcome="exhausted",
        )
        return JSONResponse({"detail": "knowledge_graph_extract_failed"}, status_code=502)
    user_name = body.user_name or "User"
    prompt = (
        f"Extract a compact knowledge graph about {user_name} from the text. Use only facts stated in the text. "
        "Return typed nodes and directed labeled edges. IDs must be stable lowercase identifiers; use 'user' for the user. "
        "Do not create self-loops or edges whose endpoints are absent.\n\nText:\n" + body.text + existing
    )
    try:
        result = await ai.run(
            getattr(env, "WORKERS_AI_KNOWLEDGE_GRAPH_MODEL", DEFAULT_WORKERS_AI_MODEL),
            {
                "messages": [
                    {"role": "system", "content": "Return only valid JSON matching the requested schema."},
                    {"role": "user", "content": prompt},
                ],
                "response_format": _extract_schema(),
                "max_tokens": 2_048,
                "temperature": 0,
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
        return JSONResponse({"detail": "knowledge_graph_extract_failed"}, status_code=502)
    mapping = _rpc_mapping(result)
    response = mapping.get("response") if mapping else None
    parsed = (
        response if isinstance(response, dict) else _structured_json(response) if isinstance(response, str) else mapping
    )
    graph = _normalized_extraction(parsed)
    if graph is None:
        record_fallback(
            component="llm",
            from_mode="workers_ai",
            to_mode="none",
            reason="malformed_doc",
            outcome="exhausted",
        )
        return JSONResponse({"detail": "knowledge_graph_extract_failed"}, status_code=502)
    return graph
