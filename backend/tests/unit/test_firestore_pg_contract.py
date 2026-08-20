from datetime import datetime, timezone

from firestore_pg import FieldFilter
from firestore_pg.client import Client, DocumentSnapshot, LastUpdateOption, Query, _build_query_sql
from firestore_pg.sql import resolve_collection


def test_resolve_collection_preserves_complete_parent_document_path():
    assert resolve_collection("users/u/conversations") == ("conversations", "users/u")
    assert resolve_collection("users/u/conversations/c1/photos") == (
        "photos",
        "users/u/conversations/c1",
    )
    assert resolve_collection("users/u/conversations/c2/photos") != resolve_collection(
        "users/u/conversations/c1/photos"
    )


def test_client_exposes_required_document_add_cursor_and_write_option_surfaces():
    client = Client(project="test", _ensure_schema=False)
    ref = client.document("users/u/conversations/c")
    assert ref.path == "users/u/conversations/c"
    assert ref.collection("photos").path == "users/u/conversations/c/photos"
    option = client.write_option(last_update_time=datetime.now(timezone.utc))
    assert isinstance(option, LastUpdateOption)
    query = client.collection("users/u/conversations").order_by("created_at")
    assert isinstance(query.start_after({"created_at": datetime.now(timezone.utc)}), Query)


def test_query_sql_uses_jsonb_ordering_and_excludes_missing_order_fields():
    sql, _ = _build_query_sql(
        "scores",
        uid="users/u",
        filters=[],
        order_bys=[("score", Query.ASCENDING)],
        limit=None,
    )
    assert "data->'score' IS NOT NULL" in sql
    assert "ORDER BY data->'score' ASC, doc_id ASC" in sql
    assert "data->>'score' ASC" not in sql


def test_not_in_sql_excludes_missing_and_json_null():
    sql, params = _build_query_sql(
        "items",
        uid="users/u",
        filters=[FieldFilter("status", "not-in", ["deleted"])],
        order_bys=[],
        limit=None,
    )
    assert "data->'status' IS NOT NULL" in sql
    assert "data->'status' <> 'null'::jsonb" in sql
    assert params["p1"] == '["deleted"]'


def test_collection_group_cursor_compares_full_document_path():
    client = Client(project="test", _ensure_schema=False)
    ref = client.document("users/u/memory_state/state")
    cursor = DocumentSnapshot(ref, exists=True, data={}, update_time=datetime.now(timezone.utc))
    sql, params = _build_query_sql(
        "memory_state",
        uid=None,
        filters=[],
        order_bys=[("__name__", Query.ASCENDING)],
        limit=100,
        collection_group=True,
        cursor=cursor,
    )
    assert "uid || '/memory_state/' || doc_id" in sql
    assert params["cursor_0"] == "users/u/memory_state/state"


def test_regular_collection_name_filter_keeps_document_id_compatibility():
    sql, params = _build_query_sql(
        "llm_usage",
        uid="users/u",
        filters=[FieldFilter("__name__", ">=", "2026-08-20")],
        order_bys=[],
        limit=None,
    )
    assert "doc_id >= :p1" in sql
    assert params["p1"] == "2026-08-20"
