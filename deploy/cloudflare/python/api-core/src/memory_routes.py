"""Canonical D1 memory routes for Better Auth accounts in the isolated staging profile.

The Cloudflare profile cannot read a Better Auth principal's historical
Firestore subtree. These routes therefore own one uid-scoped D1 authority for
accounts created inside the isolated staging environment. There is no legacy
fallback or dual write. Production promotion remains gated on the repository's
account-cutover importer and verification contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from typing import Literal
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from internal_auth import decode_context
from account_routes import usage_source_statement
from memory_review_routes import build_review_queue_statements
from vector_search import embed_query, hydrate_candidate_ids, query_vector_ids

router = APIRouter()

MAX_REQUEST_BYTES = 256_000
MAX_BATCH_REQUEST_BYTES = 8_000_000
MAX_D1_JSON_BIND_BYTES = 1_800_000
MAX_CONTENT_LENGTH = 50_000
MAX_ID_LENGTH = 256
MAX_LIST_LIMIT = 500
MAX_LIST_OFFSET = 100_000
MAX_TAGS = 100
MAX_TAG_LENGTH = 256
MAX_BATCH_DELETE = 100
MAX_BATCH_CREATE = 100
MAX_PRODUCT_SEARCH_QUERY = 500
MAX_PRODUCT_SEARCH_TOKENS = 20
MAX_PRODUCT_SEARCH_LIMIT = 500
MAX_PRODUCT_SEARCH_OFFSET = 100_000
MAX_VECTOR_SEARCH_LIMIT = 100
VECTOR_SEARCH_OVERFETCH_FACTOR = 4
ARCHIVE_CONTROL_SOURCE = "cloudflare_cutover_projection"
ARCHIVE_GLOBAL_GATE_SOURCE = "cloudflare_operator"
ARCHIVE_GLOBAL_GATE_PATH = "cloudflare:d1:cf_memory_global_read_gate"
ARCHIVE_RESTRICTED_SENSITIVITY_LABELS = (
    "credential",
    "secret",
    "financial",
    "health",
    "intimate",
    "minor",
    "minors",
    "workplace_confidential",
    "identity_authentication",
)
MEMORY_CATEGORIES = frozenset({"interesting", "system", "manual", "workflow"})
LEGACY_CATEGORY_MAP = {
    "core": "system",
    "hobbies": "system",
    "lifestyle": "system",
    "interests": "system",
    "habits": "system",
    "work": "system",
    "skills": "system",
    "learnings": "system",
    "other": "system",
    "auto": "system",
}


class MemoryCreate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    category: str = "interesting"
    visibility: Literal["public", "private"] = "private"
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    headline: str | None = Field(default=None, max_length=512)
    predicate: str | None = Field(default=None, max_length=256)
    arguments: dict[str, object] = Field(default_factory=dict)
    subject_entity_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    subject_attribution: Literal["user", "third_party", "unknown", "legacy_assumed"] = "unknown"
    object_entity_ids: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    qualifiers: dict[str, object] = Field(default_factory=dict)
    capture_confidence: float | None = Field(default=None, ge=0, le=1)
    veracity: float | None = Field(default=None, ge=0, le=1)
    uncertainty_reasons: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    durability: str | None = Field(default=None, max_length=128)

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("invalid memory category")
        normalized = value.strip().lower()
        if normalized in MEMORY_CATEGORIES:
            return normalized
        if normalized in LEGACY_CATEGORY_MAP:
            return LEGACY_CATEGORY_MAP[normalized]
        raise ValueError("invalid memory category")

    @model_validator(mode="after")
    def validate_json_fields(self) -> "MemoryCreate":
        if any(not tag or len(tag) > MAX_TAG_LENGTH for tag in self.tags):
            raise ValueError("invalid memory tag")
        if any(not item or len(item) > MAX_ID_LENGTH for item in self.object_entity_ids):
            raise ValueError("invalid object entity id")
        if any(not item or len(item) > MAX_TAG_LENGTH for item in self.uncertainty_reasons):
            raise ValueError("invalid uncertainty reason")
        for value in (self.tags, self.arguments, self.object_entity_ids, self.qualifiers, self.uncertainty_reasons):
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise ValueError("memory metadata exceeds the size limit")
        return self


class MemoryValueUpdate(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}

    value: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)


class MemoryBatchDelete(BaseModel):
    model_config = {"extra": "ignore"}

    memory_ids: list[str] = Field(default_factory=list, max_length=MAX_BATCH_DELETE)

    @model_validator(mode="after")
    def validate_ids(self) -> "MemoryBatchDelete":
        if len(self.memory_ids) != len(set(self.memory_ids)):
            raise ValueError("memory_ids must not contain duplicates")
        if any(not memory_id or len(memory_id) > MAX_ID_LENGTH for memory_id in self.memory_ids):
            raise ValueError("invalid memory id")
        return self


class MemoryBatchCreate(BaseModel):
    model_config = {"extra": "ignore"}

    memories: list[MemoryCreate] = Field(max_length=MAX_BATCH_CREATE)


class MemoryReadStatusUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    is_read: bool | None = None
    is_dismissed: bool | None = None

    @model_validator(mode="after")
    def validate_mutation(self) -> "MemoryReadStatusUpdate":
        if self.is_read is None and self.is_dismissed is None:
            raise ValueError("missing memory read mutation value")
        return self


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


async def _bounded_json(request: Request, max_bytes: int = MAX_REQUEST_BYTES) -> object:
    raw = await request.body()
    if len(raw) > max_bytes:
        raise ValueError("request body exceeds size limit")
    return json.loads(raw)


def _iso(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _bool(value: object) -> bool:
    return bool(value) and value not in ("0", "false", "False", "no")


def _json(value: object, fallback: object) -> object:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_REQUEST_BYTES:
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed


def _normalized_tag(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = "_".join(value.strip().lower().replace("-", "_").split())
    return normalized or None


def _is_per_file_local_import(tags: list[str]) -> bool:
    normalized = {tag for value in tags if (tag := _normalized_tag(value)) is not None}
    return {"local_files", "onboarding"} <= normalized and bool(
        normalized.intersection({"projects", "documents", "downloads", "recent_file"})
    )


def _json_bind_chunks(rows: list[dict[str, object]]) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    current: list[str] = []
    current_ids: list[str] = []
    current_size = 2
    for row in rows:
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        encoded_size = len(encoded.encode("utf-8"))
        if encoded_size + 2 > MAX_D1_JSON_BIND_BYTES:
            raise ValueError("memory row exceeds D1 bind limit")
        additional_size = encoded_size + (1 if current else 0)
        if current and current_size + additional_size > MAX_D1_JSON_BIND_BYTES:
            chunks.append(
                (
                    "[" + ",".join(current) + "]",
                    json.dumps(current_ids, ensure_ascii=False, separators=(",", ":")),
                )
            )
            current = []
            current_ids = []
            current_size = 2
            additional_size = encoded_size
        current.append(encoded)
        current_ids.append(str(row["id"]))
        current_size += additional_size
    if current:
        chunks.append(
            (
                "[" + ",".join(current) + "]",
                json.dumps(current_ids, ensure_ascii=False, separators=(",", ":")),
            )
        )
    return chunks


def _batch_row(uid: str, memory_id: str, memory: MemoryCreate, now: int) -> dict[str, object]:
    manually_added = memory.category == "manual"
    tier = "long_term" if manually_added or (memory.durability or "").lower() == "long_term" else "short_term"
    return {
        "uid": uid,
        "id": memory_id,
        "content": memory.content,
        "category": memory.category,
        "visibility": memory.visibility,
        "tags_json": json.dumps(memory.tags, ensure_ascii=False, separators=(",", ":")),
        "headline": memory.headline,
        "predicate": memory.predicate,
        "arguments_json": json.dumps(memory.arguments, ensure_ascii=False, separators=(",", ":")),
        "subject_entity_id": memory.subject_entity_id,
        "subject_attribution": memory.subject_attribution,
        "object_entity_ids_json": json.dumps(memory.object_entity_ids, ensure_ascii=False, separators=(",", ":")),
        "qualifiers_json": json.dumps(memory.qualifiers, ensure_ascii=False, separators=(",", ":")),
        "capture_confidence": memory.capture_confidence,
        "veracity": memory.veracity,
        "uncertainty_reasons_json": json.dumps(memory.uncertainty_reasons, ensure_ascii=False, separators=(",", ":")),
        "durability": memory.durability,
        "manually_added": int(manually_added),
        "memory_tier": tier,
        "valid_at": now,
        "created_at": now,
        "updated_at": now,
    }


def _response(row: dict[str, object]) -> dict[str, object]:
    memory_id = str(row.get("id") or "")
    tier = str(row.get("memory_tier") or "short_term")
    return {
        "id": memory_id,
        "memory_id": memory_id,
        "uid": str(row.get("uid") or ""),
        "content": str(row.get("content") or ""),
        "category": str(row.get("category") or "interesting"),
        "visibility": str(row.get("visibility") or "private"),
        "tags": _json(row.get("tags_json"), []),
        "headline": row.get("headline"),
        "predicate": row.get("predicate"),
        "arguments": _json(row.get("arguments_json"), {}),
        "subject_entity_id": row.get("subject_entity_id"),
        "subject_attribution": str(row.get("subject_attribution") or "unknown"),
        "object_entity_ids": _json(row.get("object_entity_ids_json"), []),
        "qualifiers": _json(row.get("qualifiers_json"), {}),
        "capture_confidence": row.get("capture_confidence"),
        "veracity": row.get("veracity"),
        "uncertainty_reasons": _json(row.get("uncertainty_reasons_json"), []),
        "durability": row.get("durability"),
        "conversation_id": row.get("conversation_id"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "reviewed": _bool(row.get("reviewed")),
        "user_review": None if row.get("user_review") is None else _bool(row.get("user_review")),
        "manually_added": _bool(row.get("manually_added")),
        "edited": _bool(row.get("edited")),
        "scoring": row.get("scoring"),
        "app_id": row.get("app_id"),
        "data_protection_level": row.get("data_protection_level"),
        "is_locked": _bool(row.get("is_locked")),
        "is_read": _bool(row.get("is_read")),
        "is_dismissed": _bool(row.get("is_dismissed")),
        "kg_extracted": _bool(row.get("kg_extracted")),
        "is_baseline": _bool(row.get("is_baseline")),
        "evidence": [],
        "memory_tier": tier,
        "layer": tier,
        "valid_at": _iso(row.get("valid_at")),
        "invalid_at": _iso(row.get("invalid_at")),
        "superseded_by": row.get("superseded_by"),
        "primary_capture_device": row.get("primary_capture_device"),
        "capture_device_ids": _json(row.get("capture_device_ids_json"), []),
    }


_SELECT = (
    "SELECT uid, id, content, category, visibility, tags_json, headline, predicate, arguments_json, "
    "subject_entity_id, subject_attribution, object_entity_ids_json, qualifiers_json, capture_confidence, veracity, "
    "uncertainty_reasons_json, durability, conversation_id, reviewed, user_review, manually_added, edited, scoring, "
    "app_id, data_protection_level, is_locked, is_read, is_dismissed, kg_extracted, is_baseline, memory_tier, "
    "valid_at, invalid_at, superseded_by, primary_capture_device, capture_device_ids_json, created_at, updated_at "
    "FROM cf_memories "
)


async def _first_active(env: object, uid: str, memory_id: str) -> dict[str, object] | None:
    row = (
        await env.APP_DB.prepare(_SELECT + "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL")
        .bind(uid, memory_id)
        .first()
    )
    return row if isinstance(row, dict) else None


async def _mutable_memory(env: object, uid: str, memory_id: str) -> dict[str, object] | JSONResponse:
    row = await _first_active(env, uid, memory_id)
    if row is None:
        return JSONResponse({"error": "memory not found"}, status_code=404)
    if _bool(row.get("is_locked")):
        return JSONResponse(
            {"error": "A paid plan is required to access this memory."},
            status_code=402,
        )
    return row


def _query_value(request: Request, name: str) -> str | None:
    value = request.query_params.get(name)
    return value if isinstance(value, str) else None


def _query_bool(request: Request, name: str) -> bool | None:
    value = _query_value(request, name)
    if value is None:
        return None
    normalized = value.lower()
    if normalized in {"1", "true", "on", "yes"}:
        return True
    if normalized in {"0", "false", "off", "no"}:
        return False
    return None


def _product_search_tokens(query: str) -> list[str]:
    if len(query) > MAX_PRODUCT_SEARCH_QUERY:
        raise ValueError("query exceeds size limit")
    tokens = list(
        dict.fromkeys(token.lower() for token in query.replace(".", " ").replace(",", " ").split() if len(token) > 2)
    )
    if len(tokens) > MAX_PRODUCT_SEARCH_TOKENS:
        raise ValueError("query contains too many terms")
    return tokens


def _escape_like_token(token: str) -> str:
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _product_search_item(row: dict[str, object]) -> dict[str, object]:
    updated_at = (
        _iso(row.get("updated_at"))
        or _iso(row.get("created_at"))
        or datetime.fromtimestamp(0, timezone.utc).isoformat()
    )
    confidence = row.get("capture_confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = None
    return {
        "memory_id": str(row.get("id") or ""),
        "memory_layer": "product_memory",
        "tier": str(row.get("memory_tier") or "long_term"),
        "content": str(row.get("content") or ""),
        "lifecycle_status": "active",
        "processing_state": "processed",
        "confidence": confidence,
        "visibility": row.get("visibility"),
        "visibility_source": "universal_memory_service",
        "source": row.get("conversation_id") or None,
        "date": updated_at,
        "evidence": [],
        "agent_use": "default_access_memory",
        "access_reason": "default_memory_allowed",
        "superseded_by": row.get("superseded_by"),
    }


def _product_search_policy() -> dict[str, object]:
    return {
        "consumer": "omi_chat",
        "app_has_default_memory_grant": True,
        "archive_capability": False,
        "raw_provenance_capability": False,
    }


def _product_search_rollout() -> dict[str, object]:
    capabilities = {
        "legacy_only": False,
        "shadow_artifacts_enabled": False,
        "memory_writes_enabled": True,
        "memory_reads_enabled": True,
        "legacy_reads_authoritative": False,
    }
    return {
        "consumer": "omi_chat",
        "enabled": True,
        "reason": "cloudflare_d1_authority",
        "read_decision": "USE_MEMORY",
        "mode": "read",
        "memory_reads_enabled": True,
        "legacy_reads_authoritative": False,
        "default_memory_grant": True,
        "archive_default_visible": False,
        "archive_capability": False,
        "fallback_reason": None,
        "capabilities": capabilities,
        "surface": "product_default_search",
        "archive_capability_required": False,
        "archive_capability_granted": False,
        "explicit_archive_request": False,
        "app_context": {},
    }


def _vector_search_rollout() -> dict[str, object]:
    rollout = _product_search_rollout()
    rollout.update(
        {
            "surface": "product_vector_search",
            "archive_capability_required": False,
            "archive_capability_granted": False,
            "explicit_archive_request": False,
            "app_context": {},
            "vector_repair_outbox_enabled": False,
        }
    )
    return rollout


def _product_search_gate() -> dict[str, object]:
    return {
        "source_path": "cloudflare:d1:cf_memories",
        "read_decision": "USE_MEMORY",
        "fallback_reason": None,
        "reason": "cloudflare_d1_authority",
    }


def _strict_d1_bool(value: object, field: str) -> bool:
    """Decode a D1 boolean without treating arbitrary values as enabled."""

    if type(value) is int and value in (0, 1):
        return bool(value)
    if type(value) is bool:
        return value
    raise ValueError(f"invalid {field}")


def _archive_denial(reason: str, *, gate_decision: str = "DENY_MEMORY") -> JSONResponse:
    return JSONResponse(
        {
            "error": "archive memory unavailable",
            "reason": reason,
            "archive_default_visible": False,
            "archive_capability_required": True,
            "archive_capability_granted": False,
            "global_read_gate": {
                "source_path": ARCHIVE_GLOBAL_GATE_PATH,
                "read_decision": gate_decision,
                "fallback_reason": reason,
                "reason": reason,
            },
        },
        status_code=403,
    )


async def _read_archive_authority(env: object, uid: str) -> dict[str, object] | JSONResponse:
    """Read server-owned Archive gate and capability state from D1.

    Neither table is initialized by a request.  This is intentional: an
    account must arrive through an approved cutover projection before Archive
    can be exposed, and a missing or malformed projection fails closed.
    """

    try:
        gate = await env.APP_DB.prepare(
            "SELECT id, schema_version, source, memory_reads_enabled, kill_switch_active "
            "FROM cf_memory_global_read_gate WHERE id = 1"
        ).first()
        if not isinstance(gate, dict):
            return _archive_denial("missing_global_read_gate")
        if gate.get("id") != 1 or gate.get("schema_version") != 1 or gate.get("source") != ARCHIVE_GLOBAL_GATE_SOURCE:
            return _archive_denial("malformed_global_read_gate")
        global_reads_enabled = _strict_d1_bool(gate.get("memory_reads_enabled"), "memory_reads_enabled")
        kill_switch_active = _strict_d1_bool(gate.get("kill_switch_active"), "kill_switch_active")
        if kill_switch_active:
            return _archive_denial("global_memory_read_kill_switch_active")
        if not global_reads_enabled:
            return _archive_denial("global_memory_reads_disabled")

        control = (
            await env.APP_DB.prepare(
                "SELECT uid, schema_version, source, memory_reads_enabled, default_memory_grant, "
                "archive_capability, account_generation, source_revision "
                "FROM cf_memory_control WHERE uid = ?"
            )
            .bind(uid)
            .first()
        )
        if not isinstance(control, dict) or control.get("uid") != uid:
            return _archive_denial("missing_memory_control")
        if control.get("schema_version") != 1 or control.get("source") != ARCHIVE_CONTROL_SOURCE:
            return _archive_denial("malformed_memory_control")
        if not isinstance(control.get("source_revision"), str) or not control["source_revision"].strip():
            return _archive_denial("malformed_memory_control")
        account_generation = control.get("account_generation")
        memory_reads_enabled = _strict_d1_bool(control.get("memory_reads_enabled"), "memory_reads_enabled")
        default_memory_grant = _strict_d1_bool(control.get("default_memory_grant"), "default_memory_grant")
        archive_capability = _strict_d1_bool(control.get("archive_capability"), "archive_capability")
        if type(account_generation) is not int or account_generation < 0:
            return _archive_denial("malformed_memory_control")
        if not memory_reads_enabled:
            return _archive_denial("memory_reads_disabled")
        if not default_memory_grant:
            return _archive_denial("missing_default_memory_grant")
        if not archive_capability:
            return _archive_denial("missing_archive_capability")
    except ValueError:
        return _archive_denial("malformed_memory_control")
    except Exception:
        return JSONResponse({"error": "archive memory unavailable"}, status_code=503)

    return {
        "uid": uid,
        "account_generation": account_generation,
        "source_revision": control["source_revision"],
        "global_read_gate": {
            "source_path": ARCHIVE_GLOBAL_GATE_PATH,
            "read_decision": "USE_MEMORY",
            "fallback_reason": None,
            "reason": "cloudflare_d1_authority",
        },
    }


def _archive_search_item(row: dict[str, object]) -> dict[str, object]:
    confidence = row.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = None
    evidence = _json(row.get("evidence_json"), [])
    if not isinstance(evidence, list):
        evidence = []
    updated_at = _iso(row.get("updated_at")) or _iso(row.get("created_at"))
    return {
        "memory_id": str(row.get("memory_id") or ""),
        "memory_layer": "product_memory",
        "tier": "archive",
        "content": str(row.get("content") or ""),
        "lifecycle_status": str(row.get("status") or "active"),
        "processing_state": str(row.get("processing_state") or "processed"),
        "confidence": confidence,
        "visibility": row.get("visibility"),
        "visibility_source": "memory_item.visibility",
        "source": row.get("source_id") or None,
        "date": updated_at or datetime.fromtimestamp(0, timezone.utc).isoformat(),
        "evidence": evidence,
        "agent_use": "explicit_archive_memory",
        "access_reason": "archive_explicit_allowed",
        "superseded_by": row.get("superseded_by"),
    }


@router.get("/memory/search")
async def search_product_memory(request: Request):
    """Search default-visible D1 memories for the authenticated product caller."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    query = _query_value(request, "query") or ""
    try:
        limit = int(_query_value(request, "limit") or "100")
        offset = int(_query_value(request, "offset") or "0")
        tokens = _product_search_tokens(query)
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid search parameters"}, status_code=400)
    if limit < 1 or limit > MAX_PRODUCT_SEARCH_LIMIT or offset < 0 or offset > MAX_PRODUCT_SEARCH_OFFSET:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)

    uid = str(context["uid"])
    where = (
        "WHERE uid = ? AND deleted_at IS NULL AND invalid_at IS NULL "
        "AND memory_tier != 'archive' AND COALESCE(user_review, 1) != 0 AND is_locked = 0"
    )
    args: list[object] = [uid]
    if tokens:
        clauses = ["LOWER(content) LIKE ? ESCAPE '\\'" for _ in tokens]
        where += " AND (" + " OR ".join(clauses) + ")"
        args.extend(f"%{_escape_like_token(token)}%" for token in tokens)
    env = request.scope["env"]
    try:
        count_row = (
            await env.APP_DB.prepare("SELECT COUNT(*) AS total_count FROM cf_memories " + where).bind(*args).first()
        )
        total_count = count_row.get("total_count", 0) if isinstance(count_row, dict) else 0
        if isinstance(total_count, bool) or not isinstance(total_count, int):
            total_count = int(total_count or 0)
        rows_result = (
            await env.APP_DB.prepare(_SELECT + where + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?")
            .bind(*args, limit, offset)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    rows = rows_result.get("results", []) if isinstance(rows_result, dict) else []
    items = [_product_search_item(row) for row in rows if isinstance(row, dict)]
    return {
        "uid": uid,
        "query": query,
        "items": items,
        "total_count": total_count,
        "returned_count": len(items),
        "limit": limit,
        "offset": offset,
        "archive_default_visible": False,
        "policy": _product_search_policy(),
        "global_read_gate": _product_search_gate(),
        "rollout": _product_search_rollout(),
    }


@router.get("/memory/archive/search")
async def search_archive_memory(request: Request):
    """Search the D1 Archive projection with an explicit server-owned grant.

    The projection is deliberately separate from ``cf_memories``.  That table
    is the staging default-memory authority, while this table carries the
    canonical Archive read fields and generation fence.  No legacy Firestore
    fallback exists here.
    """

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    raw_include_archive = _query_value(request, "include_archive")
    if raw_include_archive is None:
        return _archive_denial("missing_explicit_archive_request")
    include_archive = _query_bool(request, "include_archive")
    if include_archive is None:
        return JSONResponse({"error": "invalid archive request"}, status_code=400)
    if not include_archive:
        return _archive_denial("missing_explicit_archive_request")

    query = _query_value(request, "query") or ""
    try:
        limit = int(_query_value(request, "limit") or "100")
        offset = int(_query_value(request, "offset") or "0")
        tokens = _product_search_tokens(query)
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid search parameters"}, status_code=400)
    if limit < 1 or limit > MAX_PRODUCT_SEARCH_LIMIT or offset < 0 or offset > MAX_PRODUCT_SEARCH_OFFSET:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)

    uid = str(context["uid"])
    env = request.scope["env"]
    authority = await _read_archive_authority(env, uid)
    if isinstance(authority, JSONResponse):
        return authority

    restricted_placeholders = ",".join("?" for _ in ARCHIVE_RESTRICTED_SENSITIVITY_LABELS)
    where = (
        "FROM cf_memory_archive_items "
        "WHERE uid = ? AND account_generation = ? AND memory_tier = 'archive' "
        "AND status = 'active' AND processing_state = 'processed' AND source_state = 'active' "
        "AND visibility IN ('private', 'public', 'shared') AND is_locked = 0 AND deleted_at IS NULL "
        "AND json_valid(sensitivity_labels_json) = 1 AND json_type(sensitivity_labels_json) = 'array' "
        "AND json_valid(evidence_json) = 1 AND json_type(evidence_json) = 'array' "
        "AND NOT EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(sensitivity_labels_json) = 1 "
        "AND json_type(sensitivity_labels_json) = 'array' THEN sensitivity_labels_json ELSE '[]' END) AS labels "
        f"WHERE lower(trim(CAST(labels.value AS TEXT))) IN ({restricted_placeholders}))"
    )
    args: list[object] = [uid, authority["account_generation"], *ARCHIVE_RESTRICTED_SENSITIVITY_LABELS]
    if tokens:
        where += " AND (" + " OR ".join("LOWER(content) LIKE ? ESCAPE '\\'" for _ in tokens) + ")"
        args.extend(f"%{_escape_like_token(token)}%" for token in tokens)
    try:
        count_row = await env.APP_DB.prepare("SELECT COUNT(*) AS total_count " + where).bind(*args).first()
        total_count = count_row.get("total_count", 0) if isinstance(count_row, dict) else 0
        if isinstance(total_count, bool) or not isinstance(total_count, int):
            total_count = int(total_count or 0)
        rows_result = (
            await env.APP_DB.prepare(
                "SELECT uid, memory_id, content, status, processing_state, visibility, updated_at, "
                "source_id, evidence_json, confidence, superseded_by, created_at "
                + where
                + " ORDER BY updated_at DESC, memory_id DESC LIMIT ? OFFSET ?"
            )
            .bind(*args, limit, offset)
            .all()
        )
    except Exception:
        return JSONResponse({"error": "archive memories unavailable"}, status_code=503)

    rows = rows_result.get("results", []) if isinstance(rows_result, dict) else []
    items = [_archive_search_item(row) for row in rows if isinstance(row, dict)]
    return {
        "uid": uid,
        "query": query,
        "items": items,
        "total_count": total_count,
        "returned_count": len(items),
        "limit": limit,
        "offset": offset,
        "archive_default_visible": False,
        "archive_capability_required": True,
        "archive_capability_granted": True,
        "policy": {
            "consumer": "omi_chat",
            "app_has_default_memory_grant": True,
            "archive_capability": True,
            "raw_provenance_capability": False,
        },
        "global_read_gate": authority["global_read_gate"],
        "rollout": {
            "consumer": "omi_chat",
            "enabled": True,
            "reason": "cloudflare_d1_archive_authority",
            "read_decision": "USE_MEMORY",
            "mode": "read",
            "memory_reads_enabled": True,
            "legacy_reads_authoritative": False,
            "default_memory_grant": True,
            "archive_default_visible": False,
            "archive_capability": True,
            "fallback_reason": None,
            "capabilities": {
                "legacy_only": False,
                "shadow_artifacts_enabled": False,
                "memory_writes_enabled": True,
                "memory_reads_enabled": True,
                "legacy_reads_authoritative": False,
            },
            "surface": "product_archive_search",
            "archive_capability_required": True,
            "archive_capability_granted": True,
            "explicit_archive_request": True,
            "app_context": {"source_revision": authority["source_revision"]},
        },
    }


@router.get("/memory/vector/search")
async def search_vector_memory(request: Request):
    """Search default-visible memories through Vectorize candidates and D1 hydration.

    Vectorize is deliberately non-authoritative: candidate IDs are tenant
    namespaced, mapped through ``cf_vector_projection_state`` and hydrated from
    the uid-scoped D1 memory table before they enter the response.
    """

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    query = _query_value(request, "query") or ""
    try:
        limit = int(_query_value(request, "limit") or "10")
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid search parameters"}, status_code=400)
    if not query.strip() or len(query) > 4_096 or limit < 1 or limit > MAX_VECTOR_SEARCH_LIMIT:
        return JSONResponse({"error": "invalid search parameters"}, status_code=400)

    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        vector = await embed_query(env, query)
        candidate_limit = min(100, max(limit, limit * VECTOR_SEARCH_OVERFETCH_FACTOR))
        candidates = await query_vector_ids(
            env,
            "MEMORY_VECTORS",
            uid,
            vector,
            top_k=candidate_limit,
        )
        hydrated = await hydrate_candidate_ids(env, uid, "memory", candidates)
        ordered_ids = [source_id for source_id, _ in hydrated[:limit]]
        rows: list[dict[str, object]] = []
        if ordered_ids:
            placeholders = ",".join("?" for _ in ordered_ids)
            result = (
                await env.APP_DB.prepare(
                    _SELECT
                    + "WHERE uid = ? AND id IN ("
                    + placeholders
                    + ") AND deleted_at IS NULL AND invalid_at IS NULL "
                    + "AND memory_tier != 'archive' AND COALESCE(user_review, 1) != 0 AND is_locked = 0"
                )
                .bind(uid, *ordered_ids)
                .all()
            )
            raw_rows = result.get("results", []) if isinstance(result, dict) else []
            by_id = {
                str(row["id"]): row for row in raw_rows if isinstance(row, dict) and isinstance(row.get("id"), str)
            }
            rows = [by_id[memory_id] for memory_id in ordered_ids if memory_id in by_id]
    except ValueError:
        return JSONResponse({"error": "invalid search parameters"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "memory vector search unavailable"}, status_code=503)

    scores = {source_id: score for source_id, score in hydrated if source_id in {str(row["id"]) for row in rows}}
    source_versions: dict[str, str] = {}
    if scores:
        placeholders = ",".join("?" for _ in scores)
        try:
            state_result = (
                await env.APP_DB.prepare(
                    "SELECT source_id, source_version FROM cf_vector_projection_state "
                    "WHERE uid = ? AND projection_kind = 'memory' AND source_id IN (" + placeholders + ")"
                )
                .bind(uid, *scores.keys())
                .all()
            )
            state_rows = state_result.get("results", []) if isinstance(state_result, dict) else []
            source_versions = {
                str(row["source_id"]): str(row.get("source_version"))
                for row in state_rows
                if isinstance(row, dict) and isinstance(row.get("source_id"), str)
            }
        except Exception:
            return JSONResponse({"error": "memory vector search unavailable"}, status_code=503)

    items = [_response(row) for row in rows]
    rejected = max(0, len(candidates) - len(hydrated))
    return {
        "uid": uid,
        "query": query,
        "items": items,
        "scores_by_memory_id": {str(row["id"]): scores[str(row["id"])] for row in rows},
        "projection_commit_ids_by_memory_id": {
            str(row["id"]): source_versions[str(row["id"])] for row in rows if str(row["id"]) in source_versions
        },
        "decisions": {str(row["id"]): "USE_MEMORY" for row in rows},
        "total_count": len(items),
        "returned_count": len(items),
        "limit": limit,
        "overfetch_factor": VECTOR_SEARCH_OVERFETCH_FACTOR,
        "candidate_budget": candidate_limit,
        "max_vector_queries": 1,
        "max_candidate_hydration_reads": candidate_limit,
        "timeout_seconds": None,
        "candidate_request_limit": candidate_limit,
        "candidate_budget_exhausted": len(candidates) >= candidate_limit,
        "vector_query_budget_exhausted": False,
        "hydration_read_budget_exhausted": False,
        "timeout_exhausted": False,
        "search_status": "ok",
        "legacy_fallback_used": False,
        "vector_query_count": 1,
        "queried_candidate_count": len(candidates),
        "hydrated_candidate_count": len(items),
        "candidate_hydration_read_count": len(hydrated),
        "hydration_rejected_missing_count": rejected,
        "hydration_rejected_stale_projection_count": 0,
        "hydration_rejected_stale_vector_count": 0,
        "hydration_rejected_access_denied_count": max(0, len(hydrated) - len(items)),
        "vector_rejected_count": 0,
        "repair_purge_candidate_count": 0,
        "repair_purge_candidates": [],
        "repair_purge_outbox_record_count": 0,
        "repair_purge_outbox_records": [],
        "archive_default_visible": False,
        "telemetry": {"source": "cloudflare_vectorize", "projection_kind": "memory"},
        "policy": _product_search_policy(),
        "global_read_gate": _product_search_gate(),
        "rollout": _vector_search_rollout(),
    }


@router.get("/v3/memories")
async def list_memories(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        limit = int(_query_value(request, "limit") or "100")
        offset = int(_query_value(request, "offset") or "0")
    except ValueError:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    if limit < 1 or limit > MAX_LIST_LIMIT or offset < 0 or offset > MAX_LIST_OFFSET:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)

    categories_raw = _query_value(request, "categories")
    categories: list[str] = []
    if categories_raw:
        categories = list(dict.fromkeys(item.strip() for item in categories_raw.split(",") if item.strip()))
        if not categories or any(item not in MEMORY_CATEGORIES for item in categories):
            return JSONResponse({"error": "invalid memory categories"}, status_code=400)
    uid = str(context["uid"])
    query = _SELECT + "WHERE uid = ? AND deleted_at IS NULL AND invalid_at IS NULL"
    args: list[object] = [uid]
    if categories:
        query += " AND category IN (" + ",".join("?" for _ in categories) + ")"
        args.extend(categories)
    query += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
    args.extend((limit, offset))
    try:
        result = await request.scope["env"].APP_DB.prepare(query).bind(*args).all()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    rows = result.get("results", []) if isinstance(result, dict) else []
    return [_response(row) for row in rows if isinstance(row, dict)]


@router.post("/v3/memories")
async def create_memory(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        memory = MemoryCreate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid memory"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    now = int(time.time())
    memory_id = uuid.uuid4().hex
    manually_added = memory.category == "manual"
    tier = "long_term" if manually_added or (memory.durability or "").lower() == "long_term" else "short_term"
    try:
        memory_statement = env.APP_DB.prepare(
            "INSERT INTO cf_memories "
            "(uid, id, content, category, visibility, tags_json, headline, predicate, arguments_json, "
            "subject_entity_id, subject_attribution, object_entity_ids_json, qualifiers_json, capture_confidence, "
            "veracity, uncertainty_reasons_json, durability, manually_added, memory_tier, valid_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        ).bind(
            uid,
            memory_id,
            memory.content,
            memory.category,
            memory.visibility,
            json.dumps(memory.tags, ensure_ascii=False, separators=(",", ":")),
            memory.headline,
            memory.predicate,
            json.dumps(memory.arguments, ensure_ascii=False, separators=(",", ":")),
            memory.subject_entity_id,
            memory.subject_attribution,
            json.dumps(memory.object_entity_ids, ensure_ascii=False, separators=(",", ":")),
            json.dumps(memory.qualifiers, ensure_ascii=False, separators=(",", ":")),
            memory.capture_confidence,
            memory.veracity,
            json.dumps(memory.uncertainty_reasons, ensure_ascii=False, separators=(",", ":")),
            memory.durability,
            int(manually_added),
            tier,
            now,
            now,
            now,
        )
        usage_statement = usage_source_statement(
            env,
            uid=uid,
            source_kind="memory",
            source_id=memory_id,
            occurred_at=now,
            memories_created=1,
            updated_at=now,
        )
        review_statements = await build_review_queue_statements(
            env,
            uid=uid,
            candidate_rows=[_batch_row(uid, memory_id, memory, now)],
            now=now,
        )
        await env.APP_DB.batch([memory_statement, usage_statement, *review_statements])
        row = await _first_active(env, uid, memory_id)
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    if row is None:
        return JSONResponse({"error": "memory unavailable"}, status_code=503)
    return _response(row)


@router.post("/v3/memories/batch")
async def create_memories_batch(request: Request):
    """Create up to 100 memories atomically without per-item D1 queries."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        batch = MemoryBatchCreate.model_validate(await _bounded_json(request, MAX_BATCH_REQUEST_BYTES))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid memory batch"}, status_code=422)
    accepted = [memory for memory in batch.memories if not _is_per_file_local_import(memory.tags)]
    if not accepted:
        return {"memories": [], "created_count": 0}

    uid = str(context["uid"])
    now = int(time.time())
    rows = [_batch_row(uid, uuid.uuid4().hex, memory, now) for memory in accepted]
    try:
        chunks = _json_bind_chunks(rows)
    except ValueError:
        return JSONResponse({"error": "memory batch exceeds the size limit"}, status_code=413)

    env = request.scope["env"]
    statements: list[object] = []
    for rows_json, ids_json in chunks:
        statements.append(
            env.APP_DB.prepare(
                "INSERT INTO cf_memories "
                "(uid, id, content, category, visibility, tags_json, headline, predicate, arguments_json, "
                "subject_entity_id, subject_attribution, object_entity_ids_json, qualifiers_json, capture_confidence, "
                "veracity, uncertainty_reasons_json, durability, manually_added, memory_tier, valid_at, created_at, "
                "updated_at) "
                "SELECT json_extract(value, '$.uid'), json_extract(value, '$.id'), "
                "json_extract(value, '$.content'), json_extract(value, '$.category'), "
                "json_extract(value, '$.visibility'), json_extract(value, '$.tags_json'), "
                "json_extract(value, '$.headline'), json_extract(value, '$.predicate'), "
                "json_extract(value, '$.arguments_json'), json_extract(value, '$.subject_entity_id'), "
                "json_extract(value, '$.subject_attribution'), json_extract(value, '$.object_entity_ids_json'), "
                "json_extract(value, '$.qualifiers_json'), json_extract(value, '$.capture_confidence'), "
                "json_extract(value, '$.veracity'), json_extract(value, '$.uncertainty_reasons_json'), "
                "json_extract(value, '$.durability'), CAST(json_extract(value, '$.manually_added') AS INTEGER), "
                "json_extract(value, '$.memory_tier'), CAST(json_extract(value, '$.valid_at') AS INTEGER), "
                "CAST(json_extract(value, '$.created_at') AS INTEGER), "
                "CAST(json_extract(value, '$.updated_at') AS INTEGER) FROM json_each(?)"
            ).bind(rows_json)
        )
        statements.append(
            env.APP_DB.prepare(
                "INSERT INTO cf_usage_sources "
                "(uid, source_kind, source_id, occurred_at, transcription_seconds, words_transcribed, "
                "insights_gained, memories_created, updated_at) "
                "SELECT uid, 'memory', id, created_at, 0, 0, 0, 1, updated_at FROM cf_memories "
                "WHERE uid = ? AND id IN (SELECT CAST(value AS TEXT) FROM json_each(?)) "
                "ON CONFLICT(uid, source_kind, source_id) DO UPDATE SET "
                "occurred_at = excluded.occurred_at, transcription_seconds = excluded.transcription_seconds, "
                "words_transcribed = excluded.words_transcribed, insights_gained = excluded.insights_gained, "
                "memories_created = excluded.memories_created, updated_at = excluded.updated_at"
            ).bind(uid, ids_json)
        )
    try:
        statements.extend(
            await build_review_queue_statements(
                env,
                uid=uid,
                candidate_rows=rows,
                now=now,
            )
        )
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    try:
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"memories": [_response(row) for row in rows], "created_count": len(rows)}


@router.delete("/v3/memories/batch")
async def delete_memories_batch(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        deletion = MemoryBatchDelete.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid memory deletion"}, status_code=400)
    if not deletion.memory_ids:
        return {"status": "ok"}
    uid = str(context["uid"])
    env = request.scope["env"]
    placeholders = ",".join("?" for _ in deletion.memory_ids)
    try:
        rows = (
            await env.APP_DB.prepare(
                "SELECT id FROM cf_memories WHERE uid = ? AND deleted_at IS NULL AND invalid_at IS NULL "
                f"AND id IN ({placeholders})"
            )
            .bind(uid, *deletion.memory_ids)
            .all()
        )
        found = {
            str(row["id"])
            for row in (rows.get("results", []) if isinstance(rows, dict) else [])
            if isinstance(row, dict) and row.get("id")
        }
        if found != set(deletion.memory_ids):
            return JSONResponse({"error": "memory not found"}, status_code=404)
        now = int(time.time())
        statements = [
            env.APP_DB.prepare(
                "UPDATE cf_memories SET deleted_at = ?, updated_at = ? "
                "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
            ).bind(now, now, uid, memory_id)
            for memory_id in deletion.memory_ids
        ]
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}


@router.delete("/v3/memories/{memory_id}")
async def delete_memory(request: Request, memory_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not memory_id or len(memory_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "memory not found"}, status_code=404)
    uid = str(context["uid"])
    env = request.scope["env"]
    now = int(time.time())
    try:
        if await _first_active(env, uid, memory_id) is None:
            return JSONResponse({"error": "memory not found"}, status_code=404)
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET deleted_at = ?, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(now, now, uid, memory_id).run()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}


@router.delete("/v3/memories")
async def delete_all_memories(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    uid = str(context["uid"])
    now = int(time.time())
    scope = _query_value(request, "scope") or "all"
    if scope not in {"all", "default"}:
        return JSONResponse({"error": "invalid memory deletion scope"}, status_code=400)
    tier_filter = " AND memory_tier != 'archive'" if scope == "default" else ""
    try:
        await request.scope["env"].APP_DB.prepare(
            "UPDATE cf_memories SET deleted_at = ?, updated_at = ? "
            "WHERE uid = ? AND deleted_at IS NULL AND invalid_at IS NULL" + tier_filter
        ).bind(now, now, uid).run()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}


@router.patch("/v3/memories/{memory_id}")
async def update_memory_content(request: Request, memory_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    raw_value = _query_value(request, "value")
    try:
        update = (
            MemoryValueUpdate(value=raw_value)
            if raw_value is not None
            else MemoryValueUpdate.model_validate(await _bounded_json(request))
        )
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid memory content"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        if await _first_active(env, uid, memory_id) is None:
            return JSONResponse({"error": "memory not found"}, status_code=404)
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET content = ?, edited = 1, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(update.value, int(time.time()), uid, memory_id).run()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}


@router.patch("/v3/memories/{memory_id}/visibility")
async def update_memory_visibility(request: Request, memory_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    raw_value = _query_value(request, "value")
    if raw_value is None:
        try:
            raw_value = MemoryValueUpdate.model_validate(await _bounded_json(request)).value
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
            return JSONResponse({"error": "invalid memory visibility"}, status_code=400)
    if raw_value not in {"public", "private"}:
        return JSONResponse({"error": "invalid memory visibility"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        if await _first_active(env, uid, memory_id) is None:
            return JSONResponse({"error": "memory not found"}, status_code=404)
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET visibility = ?, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(raw_value, int(time.time()), uid, memory_id).run()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}


@router.post("/v3/memories/{memory_id}/review")
async def review_memory(request: Request, memory_id: str):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    raw_value = (_query_value(request, "value") or "").lower()
    if raw_value not in {"true", "false"}:
        return JSONResponse({"error": "invalid memory review"}, status_code=400)
    uid = str(context["uid"])
    env = request.scope["env"]
    value = raw_value == "true"
    try:
        if await _first_active(env, uid, memory_id) is None:
            return JSONResponse({"error": "memory not found"}, status_code=404)
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET reviewed = 1, user_review = ?, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(int(value), int(time.time()), uid, memory_id).run()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}


@router.patch("/v3/memories/{memory_id}/read")
async def update_memory_read_status(request: Request, memory_id: str):
    """Persist the durable read and dismiss state used by desktop insights."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not memory_id or len(memory_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "memory not found"}, status_code=404)
    try:
        update = MemoryReadStatusUpdate.model_validate(await _bounded_json(request))
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid memory read status"}, status_code=422)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _mutable_memory(env, uid, memory_id)
        if isinstance(existing, JSONResponse):
            return existing
        assignments: list[str] = []
        values: list[object] = []
        if update.is_read is not None:
            assignments.append("is_read = ?")
            values.append(int(update.is_read))
        if update.is_dismissed is not None:
            assignments.append("is_dismissed = ?")
            values.append(int(update.is_dismissed))
        now = int(time.time())
        assignments.append("updated_at = ?")
        values.extend((now, uid, memory_id))
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET "
            + ", ".join(assignments)
            + " WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(*values).run()
        updated = await _first_active(env, uid, memory_id)
        if not isinstance(updated, dict):
            return JSONResponse({"error": "memory not found"}, status_code=404)
        return _response(updated)
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)


@router.patch("/v3/memories/{memory_id}/baseline")
async def update_memory_baseline(request: Request, memory_id: str):
    """Update the canonical baseline flag without changing memory content."""

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not memory_id or len(memory_id) > MAX_ID_LENGTH:
        return JSONResponse({"error": "memory not found"}, status_code=404)
    value = _query_bool(request, "value")
    if value is None:
        return JSONResponse({"error": "invalid memory baseline"}, status_code=422)
    uid = str(context["uid"])
    env = request.scope["env"]
    try:
        existing = await _mutable_memory(env, uid, memory_id)
        if isinstance(existing, JSONResponse):
            return existing
        await env.APP_DB.prepare(
            "UPDATE cf_memories SET is_baseline = ?, updated_at = ? "
            "WHERE uid = ? AND id = ? AND deleted_at IS NULL AND invalid_at IS NULL"
        ).bind(int(value), int(time.time()), uid, memory_id).run()
    except Exception:
        return JSONResponse({"error": "memories unavailable"}, status_code=503)
    return {"status": "ok"}
