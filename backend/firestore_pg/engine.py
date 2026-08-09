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
from typing import Any, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

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
        return dsn.split("@")[-1]
    except Exception:  # pragma: no cover
        return dsn


def ensure_tables(engine: Optional[Engine] = None) -> None:
    """Create all known collection tables (idempotent)."""
    from .sql import build_ddl, resolve_collection
    from database.firestore_index_registry import INDEX_REQUIREMENTS

    engine = engine or get_engine()
    known_paths = dict(_KNOWN_COLLECTIONS)
    # Collection groups declared by firestore_index_registry that are not in the
    # observed set above still need their tables for compound serving queries
    # (memory maintenance, review queue, outbox, candidates, ...).
    for req in INDEX_REQUIREMENTS:
        known_paths.setdefault(req.collection_group, "")
    with engine.begin() as conn:
        for path in known_paths:
            table, _ = resolve_collection(path)
            conn.execute(text(build_ddl(table)))
    logger.info("firestore_pg: schema ensured (%d tables)", len(known_paths))


# Top-level / per-user collections observed in database/*. The uid-namespaced
# ones (users/{uid}/<coll>) are subcollection tables of the users/{uid} doc and
# are enumerated by DocumentReference.collections().
_KNOWN_COLLECTIONS: dict = {
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


_created_tables: set = set()
_created_tables_lock = threading.Lock()


def ensure_table(table: str) -> None:
    """Create a single collection table on first use (idempotent, cached)."""
    from .sql import build_ddl

    if table in _created_tables:
        return
    with get_engine().begin() as conn:
        conn.execute(text(build_ddl(table)))
    with _created_tables_lock:
        _created_tables.add(table)


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
    from .sql import resolve_collection  # noqa: F401  (table resolution lives there)

    path = field.field_path
    if path == "__name__":
        return "doc_id"
    if getattr(field, "array_config", None) == "CONTAINS":
        if "." in path:
            segs = ",".join(path.split("."))
            return f"(data #> '{{{segs}}}')"
        return f"(data -> '{path}')"
    if "." in path:
        segs = ",".join(path.split("."))
        return f"(data #>> '{{{segs}}}')"
    return f"(data ->> '{path}')"


def ensure_composite_indexes(engine: Optional[Any] = None) -> int:
    """Create PG composite indexes mirroring firestore_index_registry.

    Idempotent (CREATE INDEX IF NOT EXISTS); returns the number of index DDLs
    issued. Only requirements whose collection_group maps to a known shim table
    are created — unknown groups are skipped (a later table use can still be
    served by expression scans).
    """
    from .sql import build_ddl, resolve_collection
    from database.firestore_index_registry import INDEX_REQUIREMENTS

    engine = engine or get_engine()
    created = 0
    with engine.begin() as conn:
        for req in INDEX_REQUIREMENTS:
            table, _ = resolve_collection(req.collection_group)
            fields = [f for f in req.fields if f.field_path != "__name__"]
            if not fields:
                continue
            # Only create indexes for tables that exist (avoid creating tables
            # for never-used collection groups just to index them).
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
                # include uid first: every shim query filters on uid for
                # per-user tables; putting it first lets the btree narrow by user.
                sql = f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} USING btree (uid, {', '.join(scalar_exprs)})"
                try:
                    conn.execute(text(sql))
                    created += 1
                except Exception:  # pragma: no cover - expression/type mismatch on exotic fields
                    logger.warning("firestore_pg: skip composite index %s: %s", index_name, _short_err())
            if array_exprs:
                array_name = f"{index_name}_gin"
                sql = f"CREATE INDEX IF NOT EXISTS {array_name} ON {table} USING gin ({', '.join(array_exprs)})"
                try:
                    conn.execute(text(sql))
                    created += 1
                except Exception:  # pragma: no cover - expression/type mismatch
                    logger.warning("firestore_pg: skip gin index %s: %s", array_name, _short_err())
    if created:
        logger.info("firestore_pg: composite indexes ensured (%d)", created)
    return created


def _short_err() -> str:
    import traceback

    tb = traceback.format_exc().strip().splitlines()
    return tb[-1] if tb else "unknown"


# Thread-local transaction connection. ``@firestore.transactional`` bodies run
# on one thread (executors in this codebase are thread-backed), so a TLS slot
# keeps the same PG connection across transaction.get/set/update calls.
_local = threading.local()


def _set_tx_conn(conn: Connection) -> None:
    _local.tx_conn = conn


def _get_tx_conn() -> Optional[Connection]:
    return getattr(_local, "tx_conn", None)


def _clear_tx_conn() -> None:
    _local.tx_conn = None
