"""PostgreSQL storage layer for firestore_pg: schema bootstrap + JSONB SQL.

Top-level collections map 1:1 to tables. Nested ``users/{uid}/<coll>`` paths
map to a table named ``<coll>`` with a ``uid`` column. Every table carries:

    uid        TEXT    -- user namespace ('' for top-level collections)
    doc_id     TEXT    -- Firestore document id ('' only when uid is set)
    data       JSONB   -- full document payload
    created_at TIMESTAMPTZ

``promoted_columns`` is a registry of (collection -> {field_path: type}) that
the query translator consults to know which JSONB subfields can be queried
efficiently; unregistered paths fall back to expression queries over JSONB.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# (table, uid) resolved from a Firestore path like "users/uid/conversations/..."
_CollectionKey = Tuple[str, str]


def resolve_collection(path: str) -> _CollectionKey:
    """Split a Firestore collection path into (table_name, uid).

    Path shapes seen in the codebase:
      "users"                      -> ('users', '')
      "users/{uid}/conversations"  -> ('conversations', uid)
      "users/{uid}/folders"        -> ('folders', uid)
    Anything else is treated as a top-level table with an empty uid.
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) == 1:
        return parts[0], ""
    if len(parts) >= 3 and parts[0] == "users" and len(parts) % 2 == 1:
        # users/{uid}/<coll> (possibly users/{uid}/<coll>/... nested — drop extras)
        table = parts[2]
        for extra in range(4, len(parts), 2):
            table = f"{table}_{parts[extra]}"
        return table, parts[1]
    # Fallback: last segment is the collection, second-to-last is a doc id
    return parts[-1], ""


def _jsonb_dumps(value: Any) -> str:
    return json_dumps(value)


import json as _json


def json_dumps(value: Any) -> str:
    def _default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    return _json.dumps(value, default=_default, separators=(",", ":"))


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
    PRIMARY KEY (uid, doc_id)
);
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
        f"ON CONFLICT (uid, doc_id) DO UPDATE SET data = EXCLUDED.data"
    )


def merge_sql(table: str) -> str:
    """Partial (merge=True) update: jsonb merge of existing data + new fields.

    Firestore merge semantics: the written fields overwrite existing ones, so
    the NEW payload must win the JSONB ``||`` (right operand wins in Postgres).
    """
    return (
        f"INSERT INTO {table} (uid, doc_id, data, created_at) "
        f"VALUES (:uid, :doc_id, CAST(:data AS jsonb), now()) "
        f"ON CONFLICT (uid, doc_id) DO UPDATE SET data = {table}.data || EXCLUDED.data"
    )


def get_sql(table: str) -> str:
    return f"SELECT data FROM {table} WHERE uid = :uid AND doc_id = :doc_id"


def delete_sql(table: str) -> str:
    return f"DELETE FROM {table} WHERE uid = :uid AND doc_id = :doc_id"


def list_sql(table: str, limit: Optional[int] = None) -> str:
    sql = f"SELECT doc_id, data FROM {table} WHERE uid = :uid ORDER BY doc_id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return sql
