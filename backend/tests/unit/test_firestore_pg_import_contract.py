"""Import contract for the PostgreSQL Firestore shim."""

from __future__ import annotations

import importlib


def test_firestore_pg_sql_imports_from_the_pinned_runtime() -> None:
    """The synchronous shim must not require an undeclared async driver."""
    module = importlib.import_module("firestore_pg.sql")

    assert callable(module.resolve_collection)
