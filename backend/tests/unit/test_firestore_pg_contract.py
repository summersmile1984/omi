from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from google.api_core import exceptions as api_exceptions
from google.api_core.datetime_helpers import DatetimeWithNanoseconds

import firestore_pg.client as client_module
from firestore_pg import ArrayRemove, ArrayUnion, FieldFilter
from firestore_pg.client import (
    Client,
    DocumentSnapshot,
    LastUpdateOption,
    Query,
    UnsupportedFirestoreQuery,
    _build_query_sql,
    _unsupported_nested_map_path_guard_sql,
    _unsupported_order_guard_sql,
    transactional,
)
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
    client = Client(project="test", _verify_schema=False)
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
    assert 'ORDER BY ROW(' in sql
    assert "data->'score'" in sql
    assert 'CAST(data->>\'score\' AS numeric)' in sql
    assert '(doc_id COLLATE "C") ASC' in sql
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


def test_nan_in_and_not_in_follow_emulator_membership_semantics():
    in_sql, in_params = _build_query_sql(
        'items', uid='users/u', filters=[FieldFilter('value', 'in', [float('nan')])], order_bys=[], limit=None
    )
    not_in_sql, not_in_params = _build_query_sql(
        'items', uid='users/u', filters=[FieldFilter('value', 'not-in', [float('nan')])], order_bys=[], limit=None
    )

    assert 'FALSE' in in_sql
    assert 'p1' not in in_params
    assert "data->'value' IS NOT NULL" in not_in_sql
    assert "data->'value' <> 'null'::jsonb" in not_in_sql
    assert 'p1' not in not_in_params

    with pytest.raises(UnsupportedFirestoreQuery, match='only permits == or !='):
        _build_query_sql(
            'items',
            uid='users/u',
            filters=[FieldFilter('values', 'array-contains', float('nan'))],
            order_bys=[],
            limit=None,
        )


def test_numeric_not_equal_and_not_in_keep_other_types_but_exclude_null_or_missing():
    not_equal_sql, _ = _build_query_sql(
        'items',
        uid='users/u',
        filters=[FieldFilter('value', '!=', 1)],
        order_bys=[],
        limit=None,
    )
    not_in_sql, _ = _build_query_sql(
        'items',
        uid='users/u',
        filters=[FieldFilter('value', 'not-in', [1])],
        order_bys=[],
        limit=None,
    )

    for sql in (not_equal_sql, not_in_sql):
        assert "data->'value' IS NOT NULL" in sql
        assert "data->'value' <> 'null'::jsonb" in sql
        assert 'IS NULL OR' in sql


def test_collection_group_cursor_compares_full_document_path():
    client = Client(project="test", _verify_schema=False)
    ref = client.document("users/u/memory_state/state")
    cursor = DocumentSnapshot(ref, exists=True, data={}, update_time=datetime.now(timezone.utc))
    sql, params = _build_query_sql(
        "memory_state",
        collection_id="Memory-State",
        uid=None,
        filters=[],
        order_bys=[("__name__", Query.ASCENDING)],
        limit=100,
        collection_group=True,
        cursor=cursor,
    )
    assert "uid || '/' || :__collection_id || '/' || doc_id" in sql
    assert params["__collection_id"] == "Memory-State"
    assert params["cursor_0"] == "users/u/memory_state/state"
    assert 'COLLATE "C"' in sql


def test_regular_collection_name_filter_keeps_document_id_compatibility():
    sql, params = _build_query_sql(
        "llm_usage",
        uid="users/u",
        filters=[FieldFilter("__name__", ">=", "2026-08-20")],
        order_bys=[],
        limit=None,
    )
    assert '(doc_id COLLATE "C") >= :p1' in sql
    assert params["p1"] == "2026-08-20"


@pytest.mark.parametrize('value', [b'bytes', float('nan'), float('inf')])
def test_special_firestore_values_fail_closed_for_range_comparison(value):
    with pytest.raises(UnsupportedFirestoreQuery, match='range comparison'):
        _build_query_sql(
            'items',
            uid='users/u',
            filters=[FieldFilter('value', '>', value)],
            order_bys=[],
            limit=None,
        )


def test_timestamp_range_uses_firestore_microsecond_canonical_text_not_timestamptz():
    value = DatetimeWithNanoseconds(2026, 8, 21, 12, 34, tzinfo=timezone.utc, nanosecond=123456789)
    sql, params = _build_query_sql(
        'items',
        uid='users/u',
        filters=[FieldFilter('timestamp', '>=', value)],
        order_bys=[('timestamp', Query.ASCENDING)],
        limit=None,
    )

    assert 'timestamptz' not in sql
    assert "data->'timestamp'->>'value'" in sql
    assert params['p1'] == '2026-08-21T12:34:00.123456000Z'


@pytest.mark.parametrize(
    'malicious',
    [
        "x' IS NULL OR TRUE --",
        'x}::text[]); SELECT pg_sleep(10); --',
        '`quoted.field`',
        'x..y',
        '__firestore_pg_value__',
    ],
)
def test_query_field_paths_fail_closed_before_sql_generation(malicious):
    forged = SimpleNamespace(field_path=malicious, op_string='==', value='ignored')
    with pytest.raises(UnsupportedFirestoreQuery, match='field path'):
        _build_query_sql('items', uid='users/u', filters=[forged], order_bys=[], limit=None)
    with pytest.raises(UnsupportedFirestoreQuery, match='field path'):
        _build_query_sql('items', uid='users/u', filters=[], order_bys=[(malicious, Query.ASCENDING)], limit=None)
    with pytest.raises(UnsupportedFirestoreQuery, match='field path'):
        _unsupported_order_guard_sql('items', uid='users/u', fields=[malicious], collection_group=False)


def test_simple_dotted_field_path_is_compiled_only_from_validated_segments():
    sql, _params = _build_query_sql(
        'items',
        uid='users/u',
        filters=[FieldFilter('subject.kind', '==', 'email')],
        order_bys=[('subject.rank', Query.ASCENDING)],
        limit=None,
    )

    assert "data #> '{subject,kind}'" in sql
    assert "data #> '{subject,rank}'" in sql


def test_nested_field_path_guard_detects_ancestor_map_envelopes():
    guard = _unsupported_nested_map_path_guard_sql(
        'items',
        uid='users/u',
        fields=['m.normal', 'plain'],
        collection_group=False,
    )

    assert guard is not None
    sql, params = guard
    assert "data->'m'->>'__firestore_pg_value__'" in sql
    assert "IN ('map', 'map_entries')" in sql
    assert params == {'path_guard_uid': 'users/u'}


@pytest.mark.parametrize(
    ('operator', 'value'),
    [
        ('==', [1]),
        ('!=', {'n': 1}),
        ('in', [[1]]),
        ('not-in', [{'n': 1}]),
        ('array-contains', {'n': 1}),
        ('array-contains-any', [[1]]),
    ],
)
def test_recursive_array_or_map_query_equality_fails_closed(operator, value):
    with pytest.raises(UnsupportedFirestoreQuery, match='recursive Firestore numeric equality'):
        _build_query_sql(
            'items',
            uid='users/u',
            filters=[FieldFilter('value', operator, value)],
            order_bys=[],
            limit=None,
        )


def test_scalar_range_sql_is_type_bracketed():
    string_sql, _ = _build_query_sql(
        'items', uid='users/u', filters=[FieldFilter('value', '<', 'm')], order_bys=[], limit=None
    )
    boolean_sql, _ = _build_query_sql(
        'items', uid='users/u', filters=[FieldFilter('value', '>=', False)], order_bys=[], limit=None
    )

    assert "CASE WHEN jsonb_typeof(data->'value') = 'string'" in string_sql
    assert "CASE WHEN jsonb_typeof(data->'value') = 'boolean'" in boolean_sql


def test_array_transforms_use_recursive_firestore_numeric_and_nan_equality():
    alternate_nan = float('nan')
    current = {'values': [float('nan'), 1, {'n': 1}]}

    client_module.DocumentReference._apply_transforms(
        current,
        {'values': ArrayUnion([alternate_nan, 1.0, {'n': 1.0}, 2])},
    )
    assert len(current['values']) == 4
    client_module.DocumentReference._apply_transforms(
        current,
        {'values': ArrayRemove([float('nan'), 1.0, {'n': 1.0}])},
    )
    assert current['values'] == [2]


class _UnitConnection:
    def execute(self, _statement):
        return None


class _UnitTransactionContext:
    def __enter__(self):
        return _UnitConnection()

    def __exit__(self, _exc_type, _exc, _traceback):
        return False


class _UnitEngine:
    def begin(self):
        return _UnitTransactionContext()


def test_transactional_uses_transaction_max_attempts(monkeypatch):
    monkeypatch.setattr(client_module, 'get_engine', lambda: _UnitEngine())
    tx = Client(project='test', _verify_schema=False).transaction()
    tx._max_attempts = 5
    attempts = 0

    @transactional
    def eventually_succeeds(transaction):
        nonlocal attempts
        attempts += 1
        if attempts < 5:
            raise RuntimeError('serialization failure')
        return 'ok'

    assert eventually_succeeds(tx) == 'ok'
    assert attempts == 5


def test_transactional_exhaustion_wraps_aborted_as_firestore_value_error(monkeypatch):
    monkeypatch.setattr(client_module, 'get_engine', lambda: _UnitEngine())
    tx = Client(project='test', _verify_schema=False).transaction()
    tx._max_attempts = 2
    attempts = 0

    @transactional
    def always_conflicts(transaction):
        nonlocal attempts
        attempts += 1
        raise RuntimeError('serialization failure')

    with pytest.raises(ValueError, match='Failed to commit transaction in 2 attempts') as caught:
        always_conflicts(tx)

    assert attempts == 2
    assert isinstance(caught.value.__cause__, api_exceptions.Aborted)
