"""Connection management for firestore_pg.

Uses SQLAlchemy 2.0 (already a backend dependency) with psycopg 3 for sync
transactions. Connection parameters come from the standard PG* env vars, or
``FIRESTORE_PG_DSN`` as an override.

The design keeps a single engine shared by all shim clients; transactions are
scoped per ``@firestore.transactional`` call via a thread-local connection.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url

from database.firestore_index_registry import INDEX_REQUIREMENTS

from .field_path import parse_field_path

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                dsn = os.environ.get("FIRESTORE_PG_DSN") or _dsn_from_env()
                _engine = create_engine(dsn, pool_pre_ping=True)
                logger.info("firestore_pg: engine created for %s", _dsn_host(dsn))
    return _engine


def _dsn_from_env() -> str:
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", "omi")
    password = os.environ.get("PGPASSWORD", "")
    db = os.environ.get("PGDATABASE", "omi")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


def _dsn_host(dsn: str) -> str:
    try:
        url = make_url(dsn)
        host = url.host or '<local-socket>'
        port = f':{url.port}' if url.port is not None else ''
        database = f'/{url.database}' if url.database else ''
        return f'{host}{port}{database}'
    except Exception:  # pragma: no cover - invalid URLs fail at create_engine
        return '<invalid-postgresql-url>'


# Top-level / per-user collections observed in database/*. The uid-namespaced
# ones (users/{uid}/<coll>) are subcollection tables of the users/{uid} doc and
# are enumerated by DocumentReference.collections().
KNOWN_COLLECTIONS: dict[str, str] = {
    # top-level collections observed in database/*
    "users": "",
    "conversations": "",
    "memories": "",
    "action_items": "",
    "goals": "",
    "workstreams": "",
    "tasks": "",
    "chat_sessions": "",
    "messages": "",
    "staged_tasks": "",
    "fcm_tokens": "",
    "fair_use_state": "",
    "llm_usage": "",
    "screen_activity": "",
    "folders": "",
    "files": "",
    "events": "",
    "notifications": "",
}


# ---------------------------------------------------------------------------
# Composite indexes derived from database.firestore_index_registry
# ---------------------------------------------------------------------------
#
# The repo's Firestore index registry declares the composite indexes its
# compound queries need (database/firestore_index_registry.INDEX_REQUIREMENTS).
# The shim mirrors those as PostgreSQL expression indexes so the same queries
# are served by real indexes. Expression index columns mirror the query layer:
#   - flat field            -> (data ->> 'field')
#   - dotted field          -> (data #>> '{a,b}')
#   - '__name__' (doc id)   -> doc_id
#   - array 'CONTAINS'      -> (data -> 'field')  (gin)
#
# A registry requirement maps to a table only when its collection_group is a
# known shim table (collection-group queries query the whole table; per-user
# namespace is the uid column).


def _pg_index_expr(field: Any) -> Optional[str]:
    path = field.field_path
    if path == "__name__":
        return "doc_id"
    parsed = parse_field_path(path, allow_document_name=False)
    if getattr(field, "array_config", None) == "CONTAINS":
        if len(parsed) > 1:
            segs = ",".join(parsed)
            return f"(data #> '{{{segs}}}')"
        return f"(data -> '{next(iter(parsed))}')"
    if len(parsed) > 1:
        segs = ",".join(parsed)
        return f"(data #>> '{{{segs}}}')"
    return f"(data ->> '{next(iter(parsed))}')"


def create_composite_indexes(conn: Connection, table_name_for_collection: Callable[[str], str]) -> int:
    """Create PG composite indexes mirroring firestore_index_registry.

    Idempotent (CREATE INDEX IF NOT EXISTS); returns the number of index DDLs
    issued. Only requirements whose collection_group maps to a known shim table
    are created — unknown groups are skipped (a later table use can still be
    served by expression scans).
    """
    created = 0
    for req in INDEX_REQUIREMENTS:
        table = table_name_for_collection(req.collection_group)
        fields = [f for f in req.fields if f.field_path != "__name__"]
        if not fields:
            continue
        exists = conn.execute(
            text("SELECT 1 FROM pg_catalog.pg_class WHERE relname = :t"),
            {"t": table},
        ).fetchone()
        if not exists:
            continue
        index_name = f"idx_{table}_fs_{req.identifier[-40:]}"
        scalar_exprs = []
        array_exprs = []
        for field in fields:
            expr = _pg_index_expr(field)
            if expr is None:
                continue
            if getattr(field, "array_config", None) == "CONTAINS":
                array_exprs.append(expr)
            else:
                scalar_exprs.append(expr)
        if scalar_exprs:
            sql = f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} USING btree (uid, {', '.join(scalar_exprs)})"
            conn.execute(text(sql))
            created += 1
        if array_exprs:
            array_name = f"{index_name}_gin"
            sql = f"CREATE INDEX IF NOT EXISTS {array_name} ON {table} USING gin ({', '.join(array_exprs)})"
            conn.execute(text(sql))
            created += 1
    if created:
        logger.info("firestore_pg: composite indexes migrated (%d)", created)
    return created


# Thread-local transaction connection. ``@firestore.transactional`` bodies run
# on one thread (executors in this codebase are thread-backed), so a TLS slot
# keeps the same PG connection across transaction.get/set/update calls.
_local = threading.local()


def set_tx_conn(conn: Connection) -> None:
    _local.tx_conn = conn


def get_tx_conn() -> Optional[Connection]:
    return getattr(_local, "tx_conn", None)


def clear_tx_conn() -> None:
    _local.tx_conn = None
