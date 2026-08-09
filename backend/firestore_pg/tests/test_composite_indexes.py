"""Composite-index generation from database.firestore_index_registry.

Verifies that Client init creates PG composite indexes mirroring the repo's
Firestore index registry, including dotted-path expressions, and that the
operation is idempotent. Needs a live ``FIRESTORE_PG_DSN`` (skipped otherwise).
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("FIRESTORE_PG_DSN"), reason="needs live PostgreSQL (set FIRESTORE_PG_DSN)"
)

from firestore_pg.compat import install  # noqa: E402


@pytest.fixture(scope="module")
def engine_conn():
    install()
    from sqlalchemy import text

    from firestore_pg.engine import ensure_composite_indexes, ensure_tables, get_engine

    ensure_tables()
    engine = get_engine()
    with engine.begin() as conn:
        yield conn
    # drop only the indexes this suite created, so re-runs are deterministic
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE indexname LIKE 'idx_%_fs_%' OR indexname LIKE 'idx_%_gin%'")
        ).fetchall()
        for (name,) in rows:
            conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))


def test_registry_tables_exist(engine_conn):
    from sqlalchemy import text

    from database.firestore_index_registry import INDEX_REQUIREMENTS

    groups = {r.collection_group for r in INDEX_REQUIREMENTS}
    for group in groups:
        row = engine_conn.execute(
            text("SELECT 1 FROM pg_catalog.pg_class WHERE relname = :t"), {"t": group}
        ).fetchone()
        assert row is not None, f"table for registry collection {group} not created"


def test_composite_indexes_created(engine_conn):
    from sqlalchemy import text

    rows = engine_conn.execute(
        text("SELECT DISTINCT tablename FROM pg_indexes WHERE indexname LIKE 'idx_%_fs_%'")
    ).fetchall()
    tables = {r[0] for r in rows}
    assert "conversations" in tables, f"no composite index on conversations; have {sorted(tables)}"
    assert "memory_items" in tables, f"no composite index on memory_items; have {sorted(tables)}"


def test_dotted_path_index_expression(engine_conn):
    """chat_first_deferrals subject.{kind,id} index must use #>> (nested access),
    matching the query layer's dotted-path translation."""
    from sqlalchemy import text

    row = engine_conn.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname LIKE 'idx_%_chat_first_deferrals_by_subject%'"
        )
    ).fetchone()
    assert row is not None, "chat_first_deferrals by-subject index missing"
    indexdef = row[0]
    assert "data #>> '{subject,kind}'" in indexdef, f"dotted path not nested access: {indexdef}"


def test_index_creation_idempotent():
    """ensure_composite_indexes twice must not error (CREATE INDEX IF NOT EXISTS)."""
    from firestore_pg.engine import ensure_composite_indexes

    ensure_composite_indexes()
    ensure_composite_indexes()  # second call is a no-op
