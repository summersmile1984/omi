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

    engine = engine or get_engine()
    known_paths = {
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
    with engine.begin() as conn:
        for path in known_paths:
            table, _ = resolve_collection(path)
            conn.execute(text(build_ddl(table)))
    logger.info("firestore_pg: schema ensured (%d tables)", len(known_paths))


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
