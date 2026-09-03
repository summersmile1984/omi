"""PostgreSQL storage layer for firestore_pg: schema bootstrap + JSONB SQL.

Each collection ID maps 1:1 to a table.  The ``uid`` column is retained for
wire compatibility with the first shim revision, but now stores the complete
parent-document path.  This is important: using only the user ID cannot
distinguish e.g. ``conversations/c1/photos`` from
``conversations/c2/photos``. Every table carries:

    uid        TEXT    -- complete parent-document path ('' at top level)
    doc_id     TEXT    -- Firestore document id ('' only when uid is set)
    data       JSONB   -- full document payload
    created_at TIMESTAMPTZ
    updated_at TIMESTAMPTZ -- authoritative CAS precondition
    version    BIGINT      -- monotonically increasing document version

``promoted_columns`` is a registry of (collection -> {field_path: type}) that
the query translator consults to know which JSONB subfields can be queried
efficiently; unregistered paths fall back to expression queries over JSONB.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# (table, uid) resolved from a Firestore path like "users/uid/conversations/..."
_CollectionKey = Tuple[str, str]


def resolve_collection(path: str) -> _CollectionKey:
    """Split a Firestore collection path into (table_name, parent_path).

    Path shapes seen in the codebase:
      "users"                      -> ('users', '')
      "users/{uid}/conversations"  -> ('conversations', 'users/{uid}')
      "users/{uid}/conversations/{cid}/photos"
                                      -> ('photos', 'users/{uid}/conversations/{cid}')

    Firestore collection paths contain an odd number of segments.  Rejecting
    document paths here prevents silently placing data in the wrong namespace.
    """
    parts = [p for p in path.split("/") if p]
    if not parts or len(parts) % 2 == 0:
        raise ValueError(f"invalid Firestore collection path: {path!r}")
    return parts[-1], "/".join(parts[:-1])


def _jsonb_dumps(value: Any) -> str:
    return json_dumps(value)


import json as _json

from .codec import encode_document, encode_value


def json_dumps(value: Any) -> str:
    return _json.dumps(encode_value(value), separators=(",", ":"))


def document_dumps(value: Any) -> str:
    return _json.dumps(encode_document(value), separators=(",", ":"))


def _jsonb_loads(raw: str) -> Any:
    return _json.loads(raw) if raw else None


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

DDL_TPL = """
CREATE TABLE IF NOT EXISTS {table} (
    uid        TEXT NOT NULL DEFAULT '',
    doc_id     TEXT NOT NULL DEFAULT '',
    data       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    version    BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY (uid, doc_id)
);
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp();
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS {table}_uid_idx ON {table} (uid);
"""


def build_ddl(table: str) -> str:
    return DDL_TPL.format(table=table)


# ---------------------------------------------------------------------------
# SQL builders
# ---------------------------------------------------------------------------


def upsert_sql(table: str) -> str:
    return (
        f"INSERT INTO {table} (uid, doc_id, data, created_at) "
        f"VALUES (:uid, :doc_id, CAST(:data AS jsonb), now()) "
        f"ON CONFLICT (uid, doc_id) DO UPDATE SET "
        f"data = EXCLUDED.data, updated_at = clock_timestamp(), version = {table}.version + 1"
    )


def merge_sql(table: str) -> str:
    """Partial (merge=True) update: jsonb merge of existing data + new fields.

    Firestore merge semantics: the written fields overwrite existing ones, so
    the NEW payload must win the JSONB ``||`` (right operand wins in Postgres).
    """
    return (
        f"INSERT INTO {table} (uid, doc_id, data, created_at) "
        f"VALUES (:uid, :doc_id, CAST(:data AS jsonb), now()) "
        f"ON CONFLICT (uid, doc_id) DO UPDATE SET data = {table}.data || EXCLUDED.data, "
        f"updated_at = clock_timestamp(), version = {table}.version + 1"
    )


def get_sql(table: str) -> str:
    return f"SELECT data, updated_at, version FROM {table} WHERE uid = :uid AND doc_id = :doc_id"


def delete_sql(table: str) -> str:
    return f"DELETE FROM {table} WHERE uid = :uid AND doc_id = :doc_id"


def list_sql(table: str, limit: Optional[int] = None) -> str:
    sql = f"SELECT uid, doc_id, data, updated_at, version FROM {table} WHERE uid = :uid ORDER BY doc_id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return sql
