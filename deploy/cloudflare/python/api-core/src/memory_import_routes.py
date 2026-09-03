"""D1-backed memory-import evidence ingestion.

The import endpoint persists source artifacts only.  Extraction, acceptance,
promotion, and vector indexing remain separate workflows, matching the legacy
contract without reintroducing a Firestore or local-process dependency.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from internal_auth import decode_context

router = APIRouter()

MAX_REQUEST_BYTES = 4_000_000
MAX_ITEMS = 100
MAX_SOURCE_TYPE_LENGTH = 128
MAX_RUN_ID_LENGTH = 512
MAX_SOURCE_ACCOUNT_HASH_LENGTH = 512
MAX_IMPORTER_VERSION_LENGTH = 128
MAX_STRING_LENGTH = 50_000
MAX_EXTERNAL_ID_LENGTH = 512
MAX_CONTENT_HASH_LENGTH = 512
MAX_TITLE_LENGTH = 2_048
MAX_SNIPPET_LENGTH = 8_192
MAX_METADATA_BYTES = 65_536
MAX_DEVICE_ID_LENGTH = 256
MAX_SAFE_DOCUMENT_ID_LENGTH = 150
SAFE_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,149}$")


class RequestTooLarge(ValueError):
    """Raised before JSON parsing when the request exceeds the edge budget."""


class MemoryImportBatchItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    external_id: str | None = Field(default=None, max_length=MAX_EXTERNAL_ID_LENGTH)
    occurred_at: datetime | None = None
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    snippet: str | None = Field(default=None, max_length=MAX_SNIPPET_LENGTH)
    content: str | None = Field(default=None, max_length=MAX_STRING_LENGTH)
    content_hash: str | None = Field(default=None, max_length=MAX_CONTENT_HASH_LENGTH)
    metadata: dict[str, Any] = Field(default_factory=dict)
    client_device_id: str | None = Field(default=None, max_length=MAX_DEVICE_ID_LENGTH)

    @field_validator("external_id", "title", "snippet", "content", "content_hash", "client_device_id")
    @classmethod
    def normalize_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def require_identity_or_content(self) -> "MemoryImportBatchItem":
        if not self.external_id and not self.content_hash and not (self.content or self.snippet or self.title):
            raise ValueError("import artifact requires external_id, content_hash, or textual content")
        try:
            encoded = json.dumps(self.metadata, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("import artifact metadata is invalid") from exc
        if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
            raise ValueError("import artifact metadata exceeds the size limit")
        return self


class MemoryImportBatchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_type: str = Field(min_length=1, max_length=MAX_SOURCE_TYPE_LENGTH)
    import_run_id: str | None = Field(default=None, max_length=MAX_RUN_ID_LENGTH)
    source_account_hash: str | None = Field(default=None, max_length=MAX_SOURCE_ACCOUNT_HASH_LENGTH)
    importer_version: str = Field(default="v1", min_length=1, max_length=MAX_IMPORTER_VERSION_LENGTH)
    extractor_version: str | None = Field(default=None, max_length=MAX_IMPORTER_VERSION_LENGTH)
    items: list[MemoryImportBatchItem] = Field(default_factory=list, max_length=MAX_ITEMS)

    @field_validator("source_type", "import_run_id", "source_account_hash", "importer_version", "extractor_version")
    @classmethod
    def normalize_source_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def require_source_type(self) -> "MemoryImportBatchRequest":
        if not self.source_type:
            raise ValueError("source_type is required")
        return self


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _normalized_source_type(value: str) -> str:
    return "_".join(value.strip().lower().replace("-", "_").split())


def _document_id_from_seed(seed: str) -> str:
    raw = bytearray(hashlib.sha256(seed.encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def _safe_client_document_id(uid: str, value: str) -> str:
    if SAFE_DOCUMENT_ID.fullmatch(value):
        return value
    return _document_id_from_seed(f"memory-import-client-document-id|{uid}|{value}")


def _stable_content_hash(item: MemoryImportBatchItem) -> str:
    if item.content_hash:
        return item.content_hash
    hasher = hashlib.sha256()
    for value in (item.title, item.snippet, item.content):
        if value:
            hasher.update(value.encode("utf-8"))
            hasher.update(b"\0")
    return hasher.hexdigest()


def _artifact_id(uid: str, source_type: str, source_account_hash: str | None, item: MemoryImportBatchItem) -> str:
    identity = "|".join((source_account_hash or "", item.external_id or "", _stable_content_hash(item)))
    return _document_id_from_seed(f"memory-import-artifact|{uid}|{source_type}|{identity}")


def _run_id(uid: str, request: MemoryImportBatchRequest, source_type: str) -> str:
    if request.import_run_id:
        return _safe_client_document_id(uid, request.import_run_id)
    return _document_id_from_seed(
        "|".join(
            (
                "memory-import-run",
                uid,
                source_type,
                request.source_account_hash or "",
                request.importer_version,
            )
        )
    )


def _epoch(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


async def _bounded_json(request: Request) -> object:
    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        raise RequestTooLarge("request body exceeds size limit")
    return json.loads(body)


def _response_payload(
    run_id: str,
    received: int,
    created: int,
    deduped: int,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "artifacts_received": received,
        "artifacts_created": created,
        "artifacts_deduped": deduped,
        "candidates_created": 0,
        "status": "received",
    }


@router.post("/v3/memory-imports/batch")
async def create_memory_import_batch(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = MemoryImportBatchRequest.model_validate(await _bounded_json(request))
    except RequestTooLarge:
        return JSONResponse({"error": "memory import exceeds request size limit"}, status_code=413)
    except ValidationError:
        return JSONResponse({"error": "invalid memory import"}, status_code=400)
    except (TypeError, ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "invalid memory import"}, status_code=400)
    if len(payload.items) > MAX_ITEMS:
        return JSONResponse({"error": "memory import exceeds item limit"}, status_code=413)

    uid = str(context["uid"])
    source_type = _normalized_source_type(payload.source_type)
    if not source_type or len(source_type) > MAX_SOURCE_TYPE_LENGTH:
        return JSONResponse({"error": "invalid memory import"}, status_code=400)
    run_id = _run_id(uid, payload, source_type)
    now = int(time.time())
    env = request.scope["env"]
    full_body = getattr(env, "MEMORY_IMPORT_BODY_STORAGE_MODE", "summary") == "full"

    artifacts: list[tuple[str, MemoryImportBatchItem]] = []
    seen: set[str] = set()
    for item in payload.items:
        artifact_id = _artifact_id(uid, source_type, payload.source_account_hash, item)
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        artifacts.append((artifact_id, item))

    try:
        existing: set[str] = set()
        if artifacts:
            placeholders = ", ".join("?" for _ in artifacts)
            rows = await env.APP_DB.prepare(
                f"SELECT artifact_id FROM cf_memory_import_artifacts WHERE uid = ? AND artifact_id IN ({placeholders})"
            ).bind(uid, *(artifact_id for artifact_id, _ in artifacts)).all()
            values = rows.get("results", []) if isinstance(rows, dict) else []
            existing = {
                str(row["artifact_id"])
                for row in values
                if isinstance(row, dict) and isinstance(row.get("artifact_id"), str)
            }

        created = sum(1 for artifact_id, _ in artifacts if artifact_id not in existing)
        deduped = len(payload.items) - created
        statements = [
            env.APP_DB.prepare(
                "INSERT INTO cf_memory_import_runs "
                "(uid, run_id, source_type, source_account_hash, importer_version, extractor_version, status, "
                "artifact_count, candidate_count, accepted_count, promoted_count, deduped_count, started_at, "
                "updated_at, completed_at, last_error) VALUES "
                "(?, ?, ?, ?, ?, ?, 'received', 0, 0, 0, 0, 0, ?, ?, NULL, NULL) "
                "ON CONFLICT(uid, run_id) DO UPDATE SET updated_at = excluded.updated_at"
            ).bind(
                uid,
                run_id,
                source_type,
                payload.source_account_hash,
                payload.importer_version,
                payload.extractor_version,
                now,
                now,
            )
        ]
        for artifact_id, item in artifacts:
            redaction_status = "importer_full_excerpt" if full_body else "title_snippet_only"
            redacted_body = item.content if full_body else None
            statements.append(
                env.APP_DB.prepare(
                    "INSERT INTO cf_memory_import_artifacts "
                    "(uid, artifact_id, run_id, source_type, external_id, content_hash, title, snippet, redacted_body, "
                    "metadata_json, occurred_at, captured_at, client_device_id, source_state, redaction_status, "
                    "sensitivity_labels_json, created_at, updated_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, '[]', ?, ?) "
                    "ON CONFLICT(uid, artifact_id) DO UPDATE SET run_id = excluded.run_id, "
                    "source_state = 'active', updated_at = excluded.updated_at"
                ).bind(
                    uid,
                    artifact_id,
                    run_id,
                    source_type,
                    item.external_id,
                    _stable_content_hash(item),
                    item.title,
                    item.snippet,
                    redacted_body,
                    _json(item.metadata),
                    _epoch(item.occurred_at),
                    now,
                    item.client_device_id,
                    redaction_status,
                    now,
                    now,
                )
            )
        statements.append(
            env.APP_DB.prepare(
                "UPDATE cf_memory_import_runs SET artifact_count = artifact_count + ?, "
                "deduped_count = deduped_count + ?, updated_at = ? "
                "WHERE uid = ? AND run_id = ?"
            ).bind(created, deduped, now, uid, run_id)
        )
        await env.APP_DB.batch(statements)
    except Exception:
        return JSONResponse({"error": "memory import unavailable"}, status_code=503)

    return _response_payload(run_id, len(payload.items), created, deduped)


__all__ = ["create_memory_import_batch", "router"]
