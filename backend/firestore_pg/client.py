"""Firestore-compatible client and reference objects backed by PostgreSQL.

This module implements the object surface used by the Omi backend:

    db = firestore.Client()
    db.collection("users").document(uid).collection("conversations")...
    doc_ref.get() / set(data, merge=) / update(dict) / delete()
    query.where(filter=FieldFilter(...)).order_by(...).limit(n).stream()
    client.transaction() -> Transaction with get/set/update/delete
    @firestore.transactional

Reads return DocumentSnapshot/QuerySnapshot objects exposing ``exists``,
``to_dict()``, ``id`` — the same API surface business code already uses.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from google.api_core import exceptions as _api_exceptions
from sqlalchemy import text

from . import (
    ArrayRemove,
    ArrayUnion,
    BaseCompositeFilter,
    DELETE_FIELD,
    FieldFilter,
    Increment,
    SERVER_TIMESTAMP,
    _ensure_filter,
    _is_real_delete_field,
    _normalize_transform,
)

from .codec import (
    decode_document,
    decode_stored_document,
    encode_timestamp,
    encode_value,
    finite_double_numeric,
    unsupported_order_value_kind,
)
from .engine import (
    clear_tx_conn,
    get_engine,
    get_tx_conn,
    set_tx_conn,
)
from .field_path import UnsupportedFirestoreQuery, parse_field_path
from .migrations import COLLECTION_TABLE, check_schema, collection_table_name, require_table
from .sql import delete_sql, document_dumps, get_sql, json_dumps, merge_sql, resolve_collection, upsert_sql

logger = logging.getLogger(__name__)


_RANGE_OPERATORS = frozenset({'>', '>=', '<', '<='})
_UNSUPPORTED_ORDER_TAGS = (
    'bytes',
    'document_reference',
    'geo_point',
    'map_entries',
    'nonfinite_float',
    'string_utf8',
)

# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


class DocumentSnapshot:
    def __init__(
        self,
        reference: "DocumentReference",
        exists: bool,
        data: Optional[Mapping[str, Any]],
        update_time: Optional[datetime] = None,
    ):
        self._reference = reference
        self._exists = exists
        client = reference.parent._client if reference.parent is not None else None
        reference_factory = client.document if client is not None else None
        self._data = decode_document(dict(data), reference_factory=reference_factory) if data else {}
        self.update_time = update_time

    @property
    def exists(self) -> bool:
        return self._exists

    @property
    def id(self) -> str:
        return self._reference.id

    @property
    def reference(self) -> "DocumentReference":
        return self._reference

    def to_dict(self) -> Optional[Dict[str, Any]]:
        # An existing doc returns a dict (possibly empty); None only for a
        # missing doc — matches the real SDK's DocumentSnapshot.to_dict().
        return dict(self._data) if self._exists else None

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


class QuerySnapshot:
    def __init__(self, docs: List[DocumentSnapshot]):
        self._docs = docs

    def __iter__(self) -> Iterator[DocumentSnapshot]:
        return iter(self._docs)

    def __len__(self) -> int:
        return len(self._docs)

    def __getitem__(self, index: int) -> DocumentSnapshot:
        return self._docs[index]


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


def _write_transform(data: Mapping[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Convert sentinel field transforms into plain JSONB data.

    Returns (payload, has_transform). Sentinel values are kept in the payload
    so the transform path (``update``) can apply them atomically; ``set``/
    ``create`` callers strip them via ``_strip_sentinels``.
    """
    payload = dict(data)
    has_transform = False
    for key, value in list(payload.items()):
        value = _normalize_transform(value)
        payload[key] = value
        if isinstance(value, (_FieldTransformBase := (Increment, ArrayUnion, ArrayRemove))):
            has_transform = True
        elif value is SERVER_TIMESTAMP or isinstance(value, type(SERVER_TIMESTAMP)):
            payload[key] = datetime.now(timezone.utc)
        elif value is DELETE_FIELD or isinstance(value, type(DELETE_FIELD)):
            has_transform = True
    return payload, has_transform


def _strip_sentinels(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Remove DELETE_FIELD sentinels before dumping JSON (set/create paths)."""
    return {k: v for k, v in payload.items() if not (isinstance(v, type(DELETE_FIELD)) or _is_real_delete_field(v))}


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------


def _filter_lhs_value(field_path: str, value: Any) -> Tuple[str, Any]:
    """Resolve a filter's SQL lhs expression and the parameter value.

    Firestore's reserved ``__name__`` field is the document id (a
    ``DocumentReference`` in range queries); it maps to the ``doc_id`` column,
    not a JSONB key. DocumentReference values resolve to their id string.
    """
    if field_path == "__name__":
        if hasattr(value, "path"):
            value = value.path
        return "__name__", value
    return None, value


def _require_supported_order_value(value: Any, *, surface: str, field_path: str) -> None:
    kind = unsupported_order_value_kind(value)
    if kind is not None:
        raise UnsupportedFirestoreQuery(f'firestore_pg does not support {surface} for {kind} field {field_path!r}')


def _require_supported_exact_query_value(value: Any, *, surface: str, field_path: str) -> None:
    if isinstance(value, (list, tuple, Mapping)):
        raise UnsupportedFirestoreQuery(
            f'firestore_pg does not support {surface} for array/map value at field {field_path!r}; '
            'recursive Firestore numeric equality is not representable by direct JSONB equality'
        )


def _validate_exact_filter_value(field_filter: FieldFilter) -> None:
    op = field_filter.op_string
    if op in ('==', '!='):
        _require_supported_exact_query_value(
            field_filter.value,
            surface=f'{op} comparison',
            field_path=field_filter.field_path,
        )
    elif op in ('array-contains', 'array_contains'):
        if _is_nan(field_filter.value):
            raise UnsupportedFirestoreQuery('Firestore only permits == or != filters with NaN values')
        _require_supported_exact_query_value(
            field_filter.value,
            surface='array membership',
            field_path=field_filter.field_path,
        )
    elif op in ('in', 'not-in', 'array-contains-any', 'array_contains_any'):
        if op in ('array-contains-any', 'array_contains_any') and any(_is_nan(value) for value in field_filter.value):
            raise UnsupportedFirestoreQuery('Firestore only permits == or != filters with NaN values')
        for candidate in field_filter.value:
            _require_supported_exact_query_value(
                candidate,
                surface=f'{op} membership',
                field_path=field_filter.field_path,
            )


def _unsupported_order_guard_sql(
    table: str,
    *,
    uid: Optional[str],
    fields: Iterable[str],
    collection_group: bool,
) -> Tuple[str, Dict[str, Any]] | None:
    guarded_fields = tuple(sorted({field for field in fields if field != '__name__'}))
    if not guarded_fields:
        return None
    tag_list = ', '.join(f"'{tag}'" for tag in _UNSUPPORTED_ORDER_TAGS)
    tagged = ' OR '.join(
        '('
        f"jsonb_typeof({_json_path(field)}) = 'array' OR "
        f"(jsonb_typeof({_json_path(field)}) = 'object' AND "
        f"COALESCE({_json_path(field)}->>'__firestore_pg_value__', '') NOT IN ('timestamp', 'double')) OR "
        f"({_json_path(field)}->>'__firestore_pg_value__') IN ({tag_list}) OR "
        f"(({_json_path(field)}->>'__firestore_pg_value__') = 'double' AND "
        f"({_json_path(field)}->'value'->>'numeric') IN ('nan', 'infinity', '-infinity'))"
        ')'
        for field in guarded_fields
    )
    params: Dict[str, Any] = {}
    scope = ''
    if not collection_group:
        scope = "uid = '' AND " if uid is None else 'uid = :guard_uid AND '
        if uid is not None:
            params['guard_uid'] = uid
    return f'SELECT 1 FROM {table} WHERE {scope}({tagged}) LIMIT 1', params


def _unsupported_nested_map_path_guard_sql(
    table: str,
    *,
    uid: Optional[str],
    fields: Iterable[str],
    collection_group: bool,
) -> Tuple[str, Dict[str, Any]] | None:
    prefixes: set[str] = set()
    for field in fields:
        if field == '__name__':
            continue
        segments = parse_field_path(field, allow_document_name=False)
        prefixes.update('.'.join(segments[:length]) for length in range(1, len(segments)))
    if not prefixes:
        return None
    tagged = ' OR '.join(
        f"({_json_path(prefix)}->>'__firestore_pg_value__') IN ('map', 'map_entries')" for prefix in sorted(prefixes)
    )
    params: Dict[str, Any] = {}
    scope = ''
    if not collection_group:
        scope = "uid = '' AND " if uid is None else 'uid = :path_guard_uid AND '
        if uid is not None:
            params['path_guard_uid'] = uid
    return f'SELECT 1 FROM {table} WHERE {scope}({tagged}) LIMIT 1', params


def _assert_stored_query_values_supported(
    conn: Any,
    table: str,
    *,
    uid: Optional[str],
    filters: Sequence[FieldFilter],
    order_bys: Sequence[Tuple[str, str]],
    projection: Sequence[str] | None,
    collection_group: bool,
) -> None:
    query_fields = [item.field_path for item in filters]
    query_fields.extend(field for field, _direction in order_bys)
    query_fields.extend(projection or ())
    path_guard = _unsupported_nested_map_path_guard_sql(
        table,
        uid=uid,
        fields=query_fields,
        collection_group=collection_group,
    )
    if path_guard is not None:
        sql, params = path_guard
        if conn.execute(text(sql), params).fetchone() is not None:
            raise UnsupportedFirestoreQuery(
                'firestore_pg refuses a nested field-path query because an ancestor map contains a NUL key'
            )
    fields = [field for field, _direction in order_bys]
    fields.extend(item.field_path for item in filters if item.op_string in _RANGE_OPERATORS)
    guard = _unsupported_order_guard_sql(
        table,
        uid=uid,
        fields=fields,
        collection_group=collection_group,
    )
    if guard is None:
        return
    sql, params = guard
    if conn.execute(text(sql), params).fetchone() is not None:
        raise UnsupportedFirestoreQuery(
            'firestore_pg refuses range/order/cursor evaluation because the queried field contains '
            'an array, map, bytes, GeoPoint, document-reference, NUL string, or non-finite value'
        )


def _build_query_sql(
    table: str,
    *,
    collection_id: Optional[str] = None,
    uid: Optional[str],
    filters: List[FieldFilter],
    order_bys: List[Tuple[str, str]],
    limit: Optional[int],
    offset: Optional[int] = None,
    projection: Optional[List[str]] = None,
    collection_group: bool = False,
    cursor: Optional[Any] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Translate FieldFilter/order_by/limit into a SQL statement.

    Filters on ``data->>field`` with a JSONB containment check for
    ``array-contains``; order by uses ``data->>field`` casts for strings.
    Named parameters (``:p1``...) keep SQLAlchemy 2.0 happy.
    """
    for item in filters:
        parse_field_path(item.field_path)
        _validate_exact_filter_value(item)
    for field, _direction in order_bys:
        parse_field_path(field)
    for field in projection or ():
        parse_field_path(field, allow_document_name=False)
    where: List[str] = []
    params: Dict[str, Any] = {}
    if collection_group:
        params['__collection_id'] = collection_id or table
    if not collection_group:
        if uid is None:
            where.append("uid = ''")
        else:
            where.append("uid = :uid")
            params["uid"] = uid

    for idx, f in enumerate(filters):
        pname = f"p{idx + 1}"
        param = f":{pname}"
        op = f.op_string
        lhs, fv = _filter_lhs_value(f.field_path, f.value)
        if lhs == "__name__":
            lhs = _document_name_expr() if collection_group else _document_id_expr()
            if not collection_group and hasattr(f.value, "id"):
                fv = f.value.id
        elif lhs is None:
            lhs = _text_path(f.field_path)
        if op in ("array-contains", "array_contains"):
            if _is_finite_number(f.value):
                where.append(
                    f'EXISTS (SELECT 1 FROM jsonb_array_elements('
                    f"CASE WHEN jsonb_typeof({_json_path(f.field_path)}) = 'array' "
                    f"THEN {_json_path(f.field_path)} ELSE '[]'::jsonb END) AS actual "
                    f'WHERE {_numeric_json_value_sql("actual")} = CAST({param} AS numeric))'
                )
                params[pname] = _numeric_parameter(f.value)
            else:
                where.append(f"{_json_path(f.field_path)} @> CAST({param} AS jsonb)")
                params[pname] = json_dumps([f.value])
        elif op in ("array-contains-any", "array_contains_any"):
            values = list(f.value)
            if any(_is_nan(value) for value in values):
                raise UnsupportedFirestoreQuery(
                    'Firestore only permits == or != filters with NaN values; array membership is unsupported'
                )
            if any(_is_finite_number(value) for value in values):
                comparisons = []
                nonnumeric = []
                for value_idx, value in enumerate(values):
                    if _is_finite_number(value):
                        value_param = f'{pname}_{value_idx}'
                        comparisons.append(f'{_numeric_json_value_sql("actual")} = CAST(:{value_param} AS numeric)')
                        params[value_param] = _numeric_parameter(value)
                    else:
                        nonnumeric.append(value)
                if nonnumeric:
                    params[pname] = json_dumps(nonnumeric)
                    comparisons.append(f'actual IN (SELECT jsonb_array_elements(CAST({param} AS jsonb)))')
                where.append(
                    f'EXISTS (SELECT 1 FROM jsonb_array_elements('
                    f"CASE WHEN jsonb_typeof({_json_path(f.field_path)}) = 'array' "
                    f"THEN {_json_path(f.field_path)} ELSE '[]'::jsonb END) AS actual "
                    f"WHERE {' OR '.join(comparisons)})"
                )
            else:
                where.append(
                    f"EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof({_json_path(f.field_path)}) = 'array' "
                    f"THEN {_json_path(f.field_path)} ELSE '[]'::jsonb END) AS actual "
                    f"WHERE actual IN (SELECT jsonb_array_elements(CAST({param} AS jsonb))))"
                )
                params[pname] = json_dumps(values)
        elif op == "in":
            values_without_nan = [value for value in f.value if not _is_nan(value)]
            if not values_without_nan:
                where.append('FALSE')
                continue
            if f.field_path == "__name__":
                values = [
                    (v.path if collection_group else v.id) if hasattr(v, "path") else str(v) for v in values_without_nan
                ]
                where.append(f"{lhs} IN (SELECT unnest(CAST({param} AS text[])))")
                params[pname] = values
            elif any(_is_finite_number(value) for value in values_without_nan):
                clauses = []
                for value_idx, value in enumerate(values_without_nan):
                    value_param = f'{pname}_{value_idx}'
                    if _is_finite_number(value):
                        clauses.append(f'{_numeric_path(f.field_path)} = CAST(:{value_param} AS numeric)')
                        params[value_param] = _numeric_parameter(value)
                    else:
                        clauses.append(f'{_json_path(f.field_path)} = CAST(:{value_param} AS jsonb)')
                        params[value_param] = json_dumps(value)
                where.append('(' + ' OR '.join(clauses) + ')')
            else:
                where.append(f"{_json_path(f.field_path)} IN (SELECT jsonb_array_elements(CAST({param} AS jsonb)))")
                params[pname] = json_dumps(values_without_nan)
        elif op == "not-in":
            # Firestore excludes documents where the field is missing or null.
            if any(value is None for value in f.value):
                where.append("FALSE")
            elif not (values_without_nan := [value for value in f.value if not _is_nan(value)]):
                if f.field_path == '__name__':
                    where.append(f'{lhs} IS NOT NULL')
                else:
                    where.append(
                        f"{_json_path(f.field_path)} IS NOT NULL AND " f"{_json_path(f.field_path)} <> 'null'::jsonb"
                    )
            elif f.field_path == "__name__":
                values = [
                    (v.path if collection_group else v.id) if hasattr(v, "path") else str(v) for v in values_without_nan
                ]
                where.append(f"{lhs} NOT IN (SELECT unnest(CAST({param} AS text[])))")
                params[pname] = values
            elif any(_is_finite_number(value) for value in values_without_nan):
                clauses = []
                for value_idx, value in enumerate(values_without_nan):
                    value_param = f'{pname}_{value_idx}'
                    if _is_finite_number(value):
                        numeric = _numeric_path(f.field_path)
                        clauses.append(f'({numeric} IS NULL OR {numeric} <> CAST(:{value_param} AS numeric))')
                        params[value_param] = _numeric_parameter(value)
                    else:
                        clauses.append(f'{_json_path(f.field_path)} <> CAST(:{value_param} AS jsonb)')
                        params[value_param] = json_dumps(value)
                where.append(
                    f"{_json_path(f.field_path)} IS NOT NULL AND {_json_path(f.field_path)} <> 'null'::jsonb AND "
                    + ' AND '.join(clauses)
                )
            else:
                where.append(
                    f"{_json_path(f.field_path)} IS NOT NULL AND {_json_path(f.field_path)} <> 'null'::jsonb "
                    f"AND {_json_path(f.field_path)} NOT IN (SELECT jsonb_array_elements(CAST({param} AS jsonb)))"
                )
                params[pname] = json_dumps(values_without_nan)
        elif op in ("==", "!=") and f.field_path != "__name__" and _is_finite_number(fv):
            numeric = _numeric_path(f.field_path)
            if op == '==':
                where.append(f'{numeric} = CAST({param} AS numeric)')
            else:
                where.append(
                    f"{_json_path(f.field_path)} IS NOT NULL AND {_json_path(f.field_path)} <> 'null'::jsonb "
                    f'AND ({numeric} IS NULL OR {numeric} <> CAST({param} AS numeric))'
                )
            params[pname] = _numeric_parameter(fv)
        elif op in ("==", "!=") and f.field_path != "__name__":
            if op == '!=' and fv is None:
                where.append('FALSE')
            elif op == '!=':
                where.append(
                    f"{_json_path(f.field_path)} IS NOT NULL AND {_json_path(f.field_path)} <> 'null'::jsonb "
                    f"AND {_json_path(f.field_path)} <> CAST({param} AS jsonb)"
                )
                params[pname] = json_dumps(fv)
            else:
                where.append(f"{_json_path(f.field_path)} = CAST({param} AS jsonb)")
                params[pname] = json_dumps(fv)
        else:
            if op in _RANGE_OPERATORS:
                _require_supported_order_value(fv, surface='range comparison', field_path=f.field_path)
            if f.field_path == "__name__":
                cast_lhs, cast, comparable_value = (
                    _document_name_expr() if collection_group else _document_id_expr(),
                    None,
                    fv,
                )
            else:
                cast_lhs, cast, comparable_value = _comparison_lhs(f.field_path, fv)
            where.append(f"{cast_lhs} {_OPERATORS_SQL[op]} {param}")
            params[pname] = comparable_value
            if cast is not None:
                # CAST(:param AS <type>) keeps psycopg pyformat happy (no
                # ``::type`` suffix, which would swallow the colon as a param).
                where[-1] = f"{cast_lhs} {_OPERATORS_SQL[op]} CAST({param} AS {cast})"
            else:
                where[-1] = f"{cast_lhs} {_OPERATORS_SQL[op]} {param}"

    effective_order_bys = list(order_bys)
    if effective_order_bys and not any(field == "__name__" for field, _ in effective_order_bys):
        effective_order_bys.append(("__name__", effective_order_bys[-1][1]))
    if cursor is not None and not effective_order_bys:
        effective_order_bys = [("__name__", "ASCENDING")]

    # Firestore orderBy excludes documents that do not contain the ordered
    # field.  JSON null is a value, while a missing JSONB path is SQL NULL.
    for field, _ in effective_order_bys:
        if field != "__name__":
            where.append(f"{_json_path(field)} IS NOT NULL")

    if cursor is not None:
        cursor_order_bys = effective_order_bys
        if isinstance(cursor, Mapping) and order_bys and len(effective_order_bys) > len(order_bys):
            # Firestore permits a mapping cursor to omit its implicit __name__
            # tie-breaker. Snapshot cursors always carry it.
            cursor_order_bys = effective_order_bys[: len(order_bys)]
        cursor_values = _query_cursor_values(cursor, cursor_order_bys, collection_group=collection_group)
        for (field, _direction), value in zip(cursor_order_bys, cursor_values):
            if field != '__name__':
                _require_supported_order_value(value, surface='cursor ordering', field_path=field)
        cursor_terms: List[str] = []
        for idx, ((field, direction), value) in enumerate(zip(cursor_order_bys, cursor_values)):
            prefix: List[str] = []
            for prior_idx in range(idx):
                prior_field = cursor_order_bys[prior_idx][0]
                prior_expr = (
                    (_document_name_expr() if collection_group else _document_id_expr())
                    if prior_field == "__name__"
                    else _order_key_sql(prior_field)
                )
                prior_param = (
                    f":cursor_{prior_idx}"
                    if prior_field == '__name__'
                    else _order_key_param_sql(f'cursor_{prior_idx}', cursor_values[prior_idx], params)
                )
                prefix.append(
                    f"{prior_expr} = {prior_param}" if prior_field == "__name__" else f"{prior_expr} = {prior_param}"
                )
            expr = (
                (_document_name_expr() if collection_group else _document_id_expr())
                if field == "__name__"
                else _order_key_sql(field)
            )
            cursor_param = (
                f":cursor_{idx}" if field == '__name__' else _order_key_param_sql(f'cursor_{idx}', value, params)
            )
            comparator = ">" if direction == "ASCENDING" else "<"
            comparison = (
                f"{expr} {comparator} {cursor_param}" if field == "__name__" else f"{expr} {comparator} {cursor_param}"
            )
            cursor_terms.append("(" + " AND ".join(prefix + [comparison]) + ")")
            if field == '__name__':
                params[f"cursor_{idx}"] = value
        where.append("(" + " OR ".join(cursor_terms) + ")")

    order_sql = ""
    if effective_order_bys:
        clauses = []
        for field, direction in effective_order_bys:
            if field == "__name__":
                name_expr = _document_name_expr() if collection_group else _document_id_expr()
                clauses.append(f"{name_expr} {'ASC' if direction == 'ASCENDING' else 'DESC'}")
            else:
                # PostgreSQL JSONB ordering keeps numbers numeric (rather than
                # lexicographically sorting their text representation).
                clauses.append(f"{_order_key_sql(field)} {'ASC' if direction == 'ASCENDING' else 'DESC'}")
        order_sql = " ORDER BY " + ", ".join(clauses)

    limit_sql = f" LIMIT {int(limit)}" if limit is not None else ""
    offset_sql = f" OFFSET {int(offset)}" if offset is not None else ""

    sql = f"SELECT uid, doc_id, data, updated_at, version FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += order_sql + limit_sql + offset_sql
    return sql, params


def _document_name_expr() -> str:
    """SQL expression for Firestore's fully-qualified ``__name__`` path."""
    return "((CASE WHEN uid = '' THEN :__collection_id || '/' || doc_id ELSE uid || '/' || :__collection_id || '/' || doc_id END) COLLATE \"C\")"


def _document_id_expr() -> str:
    """Database-locale-independent regular collection document id."""
    return '(doc_id COLLATE "C")'


def _query_cursor_values(cursor: Any, order_bys: Sequence[Tuple[str, str]], *, collection_group: bool) -> List[Any]:
    """Extract ordered cursor values from a snapshot or mapping."""
    if isinstance(cursor, DocumentSnapshot) or (hasattr(cursor, "to_dict") and hasattr(cursor, "reference")):
        data = cursor.to_dict() or {}
        values = []
        for field, _ in order_bys:
            values.append(
                (cursor.reference.path if collection_group else cursor.id)
                if field == "__name__"
                else _get_path(data, field)
            )
        return values
    if isinstance(cursor, Mapping):
        values = []
        for field, _ in order_bys:
            if field not in cursor:
                raise ValueError(f"start_after cursor is missing ordered field {field!r}")
            value = cursor[field]
            if field == "__name__" and hasattr(value, "path"):
                value = value.path if collection_group else value.id
            values.append(value)
        return values
    raise TypeError("start_after expects a DocumentSnapshot or ordered-field mapping")


def _is_conflict_error(exc: Exception) -> bool:
    """True for PG serialization failures / deadlocks (retryable)."""
    msg = str(exc).lower()
    return "serializ" in msg or "deadlock" in msg


class LastUpdateOption:
    """Small equivalent of Firestore's LastUpdateOption write precondition."""

    def __init__(self, last_update_time: datetime):
        self._last_update_time = last_update_time


def _expected_update_time(option: Any) -> Optional[datetime]:
    if option is None:
        return None
    expected = getattr(option, "_last_update_time", None)
    if not isinstance(expected, datetime):
        raise TypeError("only last_update_time write preconditions are supported")
    return expected


def _split_path(key: str) -> List[str]:
    """Split a Firestore dotted field path ('a.b.c') into segments."""
    return key.split(".") if key else [key]


def _get_path(doc: Dict[str, Any], key: str) -> Any:
    cur: Any = doc
    for seg in _split_path(key):
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur[seg]
    return cur


def _set_path(doc: Dict[str, Any], key: str, value: Any) -> None:
    """Set a dotted path, creating intermediate dicts. Replaces array elements
    positionally (Firestore index-in-array semantics)."""
    segs = _split_path(key)
    cur = doc
    for seg in segs[:-1]:
        if isinstance(cur, list):
            cur = cur[int(seg)]
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    last = segs[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def _del_path(doc: Dict[str, Any], key: str) -> None:
    """Delete a dotted path; an emptied intermediate map stays as {} (matches
    Firestore, which keeps the now-empty parent object)."""
    segs = _split_path(key)
    cur: Any = doc
    for seg in segs[:-1]:
        if not isinstance(cur, dict) or seg not in cur:
            return
        cur = cur[seg]
    last = segs[-1]
    if isinstance(cur, dict) and last in cur:
        del cur[last]
    elif isinstance(cur, list) and last.isdigit() and int(last) < len(cur):
        del cur[int(last)]


def _has_dotted_key(payload: Mapping[str, Any]) -> bool:
    return any("." in str(k) for k in payload.keys())


def _run_with_conn(fn: Callable[[Any], Any], table: Optional[str] = None) -> Any:
    """Run fn on the tx connection (inside @transactional) or on an
    autocommitting engine.begin() connection otherwise."""
    if table is not None:
        require_table(table)
    engine = get_engine()
    tx_conn = get_tx_conn()
    if tx_conn is not None:
        return fn(tx_conn)
    with engine.begin() as conn:
        return fn(conn)


# Keep operator mapping here (also used above)
_OPERATORS_SQL = {
    "==": "=",
    "!=": "<>",
    ">": ">",
    ">=": ">=",
    "<": "<",
    "<=": "<=",
}


def _text_path(field_path: str) -> str:
    """SQL text expression for a JSONB value at a (possibly dotted) path.

    Firestore field paths are dot-separated (``subject.kind``); in JSONB that
    is nested access ``data #>> '{subject,kind}'``, NOT a literal key
    ``data->>'subject.kind'``. Single-segment paths keep the ``->>`` form.
    """
    parsed = parse_field_path(field_path, allow_document_name=False)
    if len(parsed) == 1:
        return f"data->>'{parsed[0]}'"
    segments = ",".join(parsed)
    return f"data #>> '{{{segments}}}'"


def _json_path(field_path: str) -> str:
    """SQL jsonb (non-text) expression for a path, for array/containment ops."""
    parsed = parse_field_path(field_path, allow_document_name=False)
    if len(parsed) == 1:
        return f"data->'{parsed[0]}'"
    segments = ",".join(parsed)
    return f"data #> '{{{segments}}}'"


def _is_finite_number(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, float) and math.isfinite(value)
    )


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _is_firestore_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _firestore_values_equal(left: Any, right: Any) -> bool:
    """Firestore value equality for array transforms.

    Python considers ``True == 1`` and every NaN unequal; Firestore has the
    opposite rules for those two cases and also applies numeric equivalence
    recursively inside arrays and maps.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if _is_firestore_number(left) or _is_firestore_number(right):
        if not (_is_firestore_number(left) and _is_firestore_number(right)):
            return False
        if isinstance(left, float) and math.isnan(left):
            return isinstance(right, float) and math.isnan(right)
        if isinstance(right, float) and math.isnan(right):
            return False
        return left == right
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        return (
            isinstance(left, (list, tuple))
            and isinstance(right, (list, tuple))
            and len(left) == len(right)
            and all(_firestore_values_equal(a, b) for a, b in zip(left, right))
        )
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        return (
            isinstance(left, Mapping)
            and isinstance(right, Mapping)
            and left.keys() == right.keys()
            and all(_firestore_values_equal(left[key], right[key]) for key in left)
        )
    try:
        return encode_value(left) == encode_value(right)
    except TypeError:
        return left == right


def _numeric_parameter(value: Any) -> str:
    if isinstance(value, float):
        return finite_double_numeric(value)
    return str(value)


def _numeric_path(field_path: str) -> str:
    encoded = _json_path(field_path)
    return _numeric_json_value_sql(encoded, text_value=_text_path(field_path))


def _numeric_json_value_sql(encoded: str, *, text_value: str | None = None) -> str:
    text_value = text_value or f"{encoded} #>> '{{}}'"
    return (
        '(CASE '
        f"WHEN jsonb_typeof({encoded}) = 'number' THEN CAST({text_value} AS numeric) "
        f"WHEN {encoded}->>'__firestore_pg_value__' = 'double' "
        f"AND {encoded}->'value'->>'numeric' NOT IN ('nan', 'infinity', '-infinity') "
        f"THEN CAST({encoded}->'value'->>'numeric' AS numeric) ELSE NULL END)"
    )


def _order_type_rank_sql(field_path: str) -> str:
    encoded = _json_path(field_path)
    numeric = _numeric_path(field_path)
    return (
        '(CASE '
        f"WHEN {encoded} = 'null'::jsonb THEN 0 "
        f"WHEN jsonb_typeof({encoded}) = 'boolean' THEN 1 "
        f'WHEN {numeric} IS NOT NULL THEN 2 '
        f"WHEN {encoded}->>'__firestore_pg_value__' = 'timestamp' THEN 3 "
        f"WHEN jsonb_typeof({encoded}) = 'string' THEN 4 "
        f"WHEN {encoded}->>'__firestore_pg_value__' IN ('bytes', 'string_utf8') THEN 5 "
        f"WHEN {encoded}->>'__firestore_pg_value__' = 'document_reference' THEN 6 "
        f"WHEN {encoded}->>'__firestore_pg_value__' = 'geo_point' THEN 7 "
        f"WHEN jsonb_typeof({encoded}) = 'array' THEN 8 ELSE 9 END)"
    )


def _order_text_sql(field_path: str) -> str:
    encoded = _json_path(field_path)
    return (
        '(CASE '
        f"WHEN {encoded}->>'__firestore_pg_value__' = 'timestamp' THEN {encoded}->>'value' "
        f"WHEN jsonb_typeof({encoded}) = 'string' THEN {_text_path(field_path)} ELSE '' END COLLATE \"C\")"
    )


def _order_key_sql(field_path: str) -> str:
    rank = _order_type_rank_sql(field_path)
    encoded = _json_path(field_path)
    return (
        f'ROW({rank}, COALESCE({_numeric_path(field_path)}, 0::numeric), {_order_text_sql(field_path)}, '
        f"CASE WHEN {rank} IN (2, 3, 4) THEN 'null'::jsonb ELSE {encoded} END)"
    )


def _python_order_key(value: Any) -> tuple[int, str, str, Any]:
    if value is None:
        return 0, '0', '', value
    if isinstance(value, bool):
        return 1, '0', '', value
    if _is_finite_number(value):
        return 2, _numeric_parameter(value), '', None
    if isinstance(value, datetime):
        return 3, '0', encode_timestamp(value), None
    if isinstance(value, str):
        return 4, '0', value, None
    encoded = encode_value(value)
    tag = encoded.get('__firestore_pg_value__') if isinstance(encoded, Mapping) else None
    if tag in {'bytes', 'string_utf8'}:
        rank = 5
    elif tag == 'document_reference':
        rank = 6
    elif tag == 'geo_point':
        rank = 7
    elif isinstance(value, (list, tuple)):
        rank = 8
    else:
        rank = 9
    return rank, '0', '', encoded


def _order_key_param_sql(prefix: str, value: Any, params: Dict[str, Any]) -> str:
    rank, numeric, text_value, encoded = _python_order_key(value)
    params[f'{prefix}_rank'] = rank
    params[f'{prefix}_numeric'] = numeric
    params[f'{prefix}_text'] = text_value
    params[f'{prefix}_json'] = json.dumps(encoded, separators=(',', ':'))
    return (
        f'ROW(:{prefix}_rank, CAST(:{prefix}_numeric AS numeric), CAST(:{prefix}_text AS text) COLLATE "C", '
        f'CAST(:{prefix}_json AS jsonb))'
    )


def _comparison_lhs(field_path: str, value: Any) -> Tuple[str, Optional[str], Any]:
    """SQL lhs for a scalar comparison and the CAST for the parameter.

    JSONB ``data->>'field'`` is text; comparing it against a timestamp,
    numeric, or boolean parameter needs matching casts on both sides. ISO-8601
    strings would compare lexicographically, which is only correct for
    timestamps in one timezone, so cast to the native type when known.
    """
    if isinstance(value, datetime):
        encoded = _json_path(field_path)
        return (
            "CASE WHEN "
            f"{encoded}->>'__firestore_pg_value__' = 'timestamp' "
            f"THEN {encoded}->>'value' ELSE NULL END",
            None,
            encode_timestamp(value),
        )
    if isinstance(value, bool):
        encoded = _json_path(field_path)
        return (
            f"CASE WHEN jsonb_typeof({encoded}) = 'boolean' THEN CAST({_text_path(field_path)} AS boolean) ELSE NULL END",
            "boolean",
            value,
        )
    if _is_finite_number(value):
        return _numeric_path(field_path), "numeric", _numeric_parameter(value)
    encoded = _json_path(field_path)
    return (
        f'(CASE WHEN jsonb_typeof({encoded}) = \'string\' THEN {_text_path(field_path)} ELSE NULL END COLLATE "C")',
        None,
        value,
    )


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


class Query:
    # Firestore SDK class constants used by business code as
    # ``firestore.Query.ASCENDING / DESCENDING`` (12+ database/* modules).
    ASCENDING = "ASCENDING"
    DESCENDING = "DESCENDING"

    def __init__(
        self,
        *,
        table: str,
        collection_id: Optional[str] = None,
        uid: Optional[str] = None,
        parent: Optional["CollectionReference"] = None,
        filters: Optional[List[FieldFilter]] = None,
        order_bys: Optional[List[Tuple[str, str]]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        projection: Optional[List[str]] = None,
        collection_group: bool = False,
        cursor: Optional[Any] = None,
    ):
        self._table = table
        self._collection_id = collection_id or table
        self._uid = uid
        self._parent = parent
        self._filters = list(filters or [])
        self._order_bys = list(order_bys or [])
        self._limit = limit
        self._offset = offset
        self._projection = projection
        self._collection_group = collection_group
        self._cursor = cursor

    def where(self, *args: Any, filter: Any = None, **kwargs: Any) -> "Query":
        # Accept both the shim's BaseCompositeFilter and the real SDK's
        # (bound by modules imported before compat.install) — both expose
        # .operator / .filters.
        is_composite = isinstance(filter, BaseCompositeFilter) or (
            filter is not None
            and hasattr(filter, "filters")
            and hasattr(filter, "operator")
            and "omposite" in type(filter).__name__
        )
        if is_composite:
            if filter.operator != BaseCompositeFilter.AND:
                raise ValueError(f"shim supports only AND composite filters, got {filter.operator!r}")
            extra = [_ensure_filter(f) for f in filter.filters]
            return Query(
                table=self._table,
                collection_id=self._collection_id,
                uid=self._uid,
                parent=self._parent,
                filters=self._filters + extra,
                order_bys=self._order_bys,
                limit=self._limit,
                offset=self._offset,
                projection=self._projection,
                collection_group=self._collection_group,
                cursor=self._cursor,
            )
        if filter is not None:
            f = _ensure_filter(filter)
        elif len(args) == 3:
            # legacy string API: where('field', '==', value)
            f = _ensure_filter(list(args))
        else:
            f = _ensure_filter(args[0])
        return Query(
            table=self._table,
            collection_id=self._collection_id,
            uid=self._uid,
            parent=self._parent,
            filters=self._filters + [f],
            order_bys=self._order_bys,
            limit=self._limit,
            offset=self._offset,
            projection=self._projection,
            collection_group=self._collection_group,
            cursor=self._cursor,
        )

    def order_by(self, field_path: str, direction: Optional[str] = None) -> "Query":
        parse_field_path(field_path)
        direction = direction or "ASCENDING"
        return Query(
            table=self._table,
            collection_id=self._collection_id,
            uid=self._uid,
            parent=self._parent,
            filters=self._filters,
            order_bys=self._order_bys + [(field_path, direction)],
            limit=self._limit,
            offset=self._offset,
            projection=self._projection,
            collection_group=self._collection_group,
            cursor=self._cursor,
        )

    def limit(self, count: int) -> "Query":
        return Query(
            table=self._table,
            collection_id=self._collection_id,
            uid=self._uid,
            parent=self._parent,
            filters=self._filters,
            order_bys=self._order_bys,
            limit=count,
            offset=self._offset,
            projection=self._projection,
            collection_group=self._collection_group,
            cursor=self._cursor,
        )

    def offset(self, count: int) -> "Query":
        return Query(
            table=self._table,
            collection_id=self._collection_id,
            uid=self._uid,
            parent=self._parent,
            filters=self._filters,
            order_bys=self._order_bys,
            limit=self._limit,
            offset=count,
            projection=self._projection,
            collection_group=self._collection_group,
            cursor=self._cursor,
        )

    def select(self, field_paths: List[str]) -> "Query":
        """Projection: only return the listed top-level fields."""
        for field_path in field_paths:
            parse_field_path(field_path, allow_document_name=False)
        return Query(
            table=self._table,
            collection_id=self._collection_id,
            uid=self._uid,
            parent=self._parent,
            filters=self._filters,
            order_bys=self._order_bys,
            limit=self._limit,
            offset=self._offset,
            projection=list(field_paths),
            collection_group=self._collection_group,
            cursor=self._cursor,
        )

    def start_after(self, document_fields: Any) -> "Query":
        return Query(
            table=self._table,
            collection_id=self._collection_id,
            uid=self._uid,
            parent=self._parent,
            filters=self._filters,
            order_bys=self._order_bys,
            limit=self._limit,
            offset=self._offset,
            projection=self._projection,
            collection_group=self._collection_group,
            cursor=document_fields,
        )

    def count(self) -> "AggregationQuery":
        """Return an aggregation query whose ``.get()`` yields the row count.

        Mirrors Firestore's ``Query.count()`` -> ``AggregationQuery`` shape:
        ``count_query.get()`` returns a list with a single result carrying
        ``.value`` (the integer count).
        """
        return AggregationQuery(self)

    def stream(self, transaction: Optional["Transaction"] = None, **kwargs: Any) -> Iterator[DocumentSnapshot]:
        # Firestore callers pass retry=/timeout= kwargs (trends.py etc.); the
        # shim ignores retry policy (SQL is deterministic) but must accept it.
        if transaction is not None:
            return self._run(transaction=transaction)
        return self._run()

    def get(self, transaction: Optional["Transaction"] = None, **kwargs: Any) -> QuerySnapshot:
        if transaction is not None:
            return QuerySnapshot(list(self._run(transaction=transaction)))
        return QuerySnapshot(list(self._run()))

    def _run(self, transaction: Optional["Transaction"] = None) -> Iterator[DocumentSnapshot]:
        engine = get_engine()
        if transaction is not None and transaction._conn is not None:
            tx_conn = transaction._conn
        else:
            tx_conn = get_tx_conn()
        sql, params = _build_query_sql(
            self._table,
            collection_id=self._collection_id,
            uid=self._uid,
            filters=self._filters,
            order_bys=self._order_bys,
            limit=self._limit,
            offset=self._offset,
            projection=self._projection,
            collection_group=self._collection_group,
            cursor=self._cursor,
        )
        require_table(self._table)

        def execute(conn: Any) -> List[Any]:
            _assert_stored_query_values_supported(
                conn,
                self._table,
                uid=self._uid,
                filters=self._filters,
                order_bys=self._order_bys,
                projection=self._projection,
                collection_group=self._collection_group,
            )
            return list(conn.execute(text(sql), params).fetchall())

        if tx_conn is not None:
            rows = execute(tx_conn)
        else:
            with engine.begin() as conn:
                rows = execute(conn)
        for row in rows:
            namespace, doc_id, raw, update_time, _version = row
            if self._projection is not None:
                raw = {k: raw[k] for k in self._projection if k in raw}
            collection_path = f"{namespace}/{self._collection_id}" if namespace else self._collection_id
            parent = (
                self._parent
                if not self._collection_group
                else CollectionReference(
                    collection_path,
                    client=self._parent._client if self._parent is not None else None,
                    table=self._table,
                    collection_id=self._collection_id,
                    uid=namespace,
                )
            )
            yield DocumentSnapshot(
                DocumentReference(
                    self._table,
                    namespace,
                    doc_id,
                    parent=parent,
                    collection_id=self._collection_id,
                ),
                exists=True,
                data=raw,
                update_time=update_time,
            )


class _AggregationResult:
    """A single Firestore aggregation result with a numeric ``.value``."""

    def __init__(self, value: int) -> None:
        self.value = value


class AggregationQuery:
    """``Query.count()`` result: ``.get()`` returns ``[_AggregationResult]``."""

    def __init__(self, query: "Query") -> None:
        self._query = query

    def get(self) -> List[_AggregationResult]:
        # Reuse the complete query. Firestore aggregations honor the source
        # query's cursor, limit, and offset.
        sql, params = _build_query_sql(
            self._query._table,
            collection_id=self._query._collection_id,
            uid=self._query._uid,
            filters=self._query._filters,
            order_bys=self._query._order_bys,
            limit=self._query._limit,
            offset=self._query._offset,
            collection_group=self._query._collection_group,
            cursor=self._query._cursor,
        )
        count_sql = f"SELECT count(*) FROM ({sql}) AS _agg"
        require_table(self._query._table)

        def execute(conn: Any) -> int:
            _assert_stored_query_values_supported(
                conn,
                self._query._table,
                uid=self._query._uid,
                filters=self._query._filters,
                order_bys=self._query._order_bys,
                projection=self._query._projection,
                collection_group=self._query._collection_group,
            )
            return int(conn.execute(text(count_sql), params).fetchone()[0])

        tx_conn = get_tx_conn()
        if tx_conn is not None:
            total = execute(tx_conn)
        else:
            with get_engine().begin() as conn:
                total = execute(conn)
        return [[_AggregationResult(int(total))]]


class CollectionReference:
    def __init__(
        self,
        path: str,
        client: Optional["Client"] = None,
        table: Optional[str] = None,
        collection_id: Optional[str] = None,
        uid: Optional[str] = None,
    ):
        self._path = path
        self._client = client
        if table is not None:
            self._table, self._uid = table, uid
            self._collection_id = collection_id or resolve_collection(path)[0]
        else:
            logical_id, self._uid = resolve_collection(path)
            self._collection_id = logical_id
            self._table = collection_table_name(logical_id)

    @property
    def id(self) -> str:
        return self._collection_id

    @property
    def path(self) -> str:
        return self._path

    def document(self, document_id: Optional[str] = None) -> "DocumentReference":
        if document_id is None:
            document_id = uuid.uuid4().hex
        return DocumentReference(
            self._table,
            self._uid,
            document_id,
            parent=self,
            collection_id=self._collection_id,
        )

    def add(
        self, document_data: Mapping[str, Any], document_id: Optional[str] = None, **kwargs: Any
    ) -> Tuple[datetime, "DocumentReference"]:
        ref = self.document(document_id)
        ref.create(document_data)
        snapshot = ref.get()
        if snapshot.update_time is None:  # pragma: no cover - persisted rows always carry it
            raise RuntimeError(f"created document {ref.path} has no update_time")
        return snapshot.update_time, ref

    def stream(self, **kwargs: Any) -> Iterator[DocumentSnapshot]:
        return self._query().stream(**kwargs)

    def get(self) -> QuerySnapshot:
        return self._query().get()

    def where(self, *args: Any, filter: Any = None, **kwargs: Any) -> Query:
        return self._query().where(*args, filter=filter, **kwargs)

    def order_by(self, field_path: str, direction: Optional[str] = None) -> Query:
        return self._query().order_by(field_path, direction)

    def limit(self, count: int) -> Query:
        return self._query().limit(count)

    def count(self) -> "AggregationQuery":
        return self._query().count()

    def select(self, field_paths: List[str]) -> Query:
        return self._query().select(field_paths)

    def _query(self) -> Query:
        return Query(table=self._table, collection_id=self._collection_id, uid=self._uid, parent=self)


class DocumentReference:
    def __init__(
        self,
        table: str,
        uid: Optional[str],
        document_id: str,
        parent: Optional[CollectionReference] = None,
        path: Optional[str] = None,
        collection_id: Optional[str] = None,
    ):
        self._table = table
        self._collection_id = collection_id or (parent.id if parent is not None else table)
        self._uid = uid
        self._id = document_id
        self._parent = parent
        self._path = path

    @property
    def id(self) -> str:
        return self._id

    @property
    def path(self) -> str:
        if self._path is not None:
            return self._path
        if self._parent is not None:
            return f"{self._parent._path}/{self._id}"
        collection_path = f"{self._uid}/{self._collection_id}" if self._uid else self._collection_id
        return f"{collection_path}/{self._id}"

    @property
    def parent(self) -> Optional[CollectionReference]:
        return self._parent

    def collection(self, collection_path: str) -> CollectionReference:
        # children of users/{uid}/<coll>/{doc} live at a path that
        # resolve_collection maps back to a (table, uid) namespace
        return CollectionReference(
            f"{self.path}/{collection_path}",
            client=self._parent._client if self._parent is not None else None,
        )

    def collections(self) -> List[CollectionReference]:
        """Return the subcollections of this document.

        Discover table-backed collection IDs that contain a document in this
        exact parent namespace. This mirrors Firestore's live collection-ID
        enumeration and means account deletion automatically sees collections
        introduced by future upstream code.
        """
        parent_path = self.path
        with get_engine().begin() as conn:
            rows = conn.execute(
                text(f"SELECT collection_id, table_name FROM {COLLECTION_TABLE} ORDER BY collection_id")
            ).fetchall()
            candidates = [(str(collection_id), str(table)) for collection_id, table in rows]
            if candidates:
                # One round trip with indexed existence probes, rather than a
                # query per table for every document visited by recursive
                # account deletion.
                params: dict[str, Any] = {'path': parent_path}
                probes = []
                for index, (collection_id, table) in enumerate(candidates):
                    params[f'collection_id_{index}'] = collection_id
                    probes.append(
                        f'(SELECT :collection_id_{index} AS collection_id FROM {table} WHERE uid = :path LIMIT 1)'
                    )
                tables = [row[0] for row in conn.execute(text(' UNION ALL '.join(probes)), params).fetchall()]
            else:  # pragma: no cover - a migrated schema always registers known tables
                tables = []
        return [
            CollectionReference(f"{parent_path}/{table}", client=self._parent._client if self._parent else None)
            for table in sorted(tables)
        ]

    def get(
        self,
        field_paths: Optional[List[str]] = None,
        transaction: Optional["Transaction"] = None,
        **kwargs: Any,
    ) -> DocumentSnapshot:
        if transaction is not None:
            return transaction.get(self, field_paths=field_paths)
        row = _run_with_conn(
            lambda conn: conn.execute(
                text(get_sql(self._table)), {"uid": self._uid or "", "doc_id": self._id}
            ).fetchone(),
            table=self._table,
        )
        if row is None:
            return DocumentSnapshot(self, exists=False, data=None)
        data, update_time, _version = row
        if field_paths is not None:
            data = {k: data[k] for k in field_paths if k in data}
        return DocumentSnapshot(self, exists=True, data=data, update_time=update_time)

    def set(self, document_data: Mapping[str, Any], merge: bool = False) -> "DocumentReference":
        payload, has_transform = _write_transform(document_data)
        if merge and (has_transform or _has_dotted_key(payload)):
            # merge=True with transforms (e.g. record_user_platform): merge the
            # plain fields, then apply transforms against the merged doc
            return self._merge_with_transforms(payload)
        if has_transform:
            materialized: Dict[str, Any] = {}
            self._apply_transforms(materialized, payload)
            payload = materialized
        payload = _strip_sentinels(payload)
        sql = merge_sql(self._table) if merge else upsert_sql(self._table)
        _run_with_conn(
            lambda conn: conn.execute(
                text(sql), {"uid": self._uid or "", "doc_id": self._id, "data": document_dumps(payload)}
            ),
            table=self._table,
        )
        return self

    def update(self, field_updates: Mapping[str, Any], option: Any = None, **kwargs: Any) -> "DocumentReference":
        payload, has_transform = _write_transform(field_updates)
        if has_transform:
            # read-modify-write under the transaction's lock (or a short lock)
            return self._update_with_transforms(payload, option=option)

        def _do(conn: Any) -> None:
            row = self._read_row_for_update(conn)
            if row is None:
                raise _api_exceptions.NotFound(f"Document {self.path} does not exist")
            self._check_precondition(row[1], option)
            merged = decode_stored_document(row[0] or {})
            if _has_dotted_key(payload):
                for k, v in payload.items():
                    _set_path(merged, k, v)
            else:
                merged.update(payload)
            self._write_existing(conn, merged)

        _run_with_conn(_do, table=self._table)
        return self

    @staticmethod
    def _apply_transforms(current: Dict[str, Any], payload: Dict[str, Any]) -> None:
        for key, value in payload.items():
            dotted = "." in str(key)
            if isinstance(value, Increment):
                cur = _get_path(current, key) if dotted else current.get(key)
                base = cur if isinstance(cur, (int, float)) and not isinstance(cur, bool) else 0
                _set_path(current, key, base + value.value)
            elif isinstance(value, ArrayUnion):
                cur = _get_path(current, key) if dotted else current.get(key)
                arr = list(cur or [])
                for item in value.value:
                    if not any(_firestore_values_equal(item, existing) for existing in arr):
                        arr.append(item)
                _set_path(current, key, arr)
            elif isinstance(value, ArrayRemove):
                cur = _get_path(current, key) if dotted else current.get(key)
                arr = list(cur or [])
                _set_path(
                    current,
                    key,
                    [
                        item
                        for item in arr
                        if not any(_firestore_values_equal(item, removed) for removed in value.value)
                    ],
                )
            elif isinstance(value, type(DELETE_FIELD)) or _is_real_delete_field(value):
                _del_path(current, key)
            else:
                _set_path(current, key, value)

    def _read_row_for_update(self, conn: Any) -> Any:
        return conn.execute(
            text(get_sql(self._table) + " FOR UPDATE"),
            {"uid": self._uid or "", "doc_id": self._id},
        ).fetchone()

    def _write_existing(self, conn: Any, current: Dict[str, Any]) -> None:
        conn.execute(
            text(
                f"UPDATE {self._table} SET data = CAST(:data AS jsonb), "
                "updated_at = clock_timestamp(), version = version + 1 "
                "WHERE uid = :uid AND doc_id = :doc_id"
            ),
            {"uid": self._uid or "", "doc_id": self._id, "data": document_dumps(current)},
        )

    @staticmethod
    def _check_precondition(actual_update_time: datetime, option: Any) -> None:
        expected = _expected_update_time(option)
        if expected is not None and actual_update_time != expected:
            raise _api_exceptions.FailedPrecondition("document update_time precondition failed")

    def _update_with_transforms(self, payload: Dict[str, Any], option: Any = None) -> "DocumentReference":
        def _do(conn: Any) -> None:
            row = self._read_row_for_update(conn)
            if row is None:
                raise _api_exceptions.NotFound(f"Document {self.path} does not exist")
            self._check_precondition(row[1], option)
            current = decode_stored_document(row[0] or {})
            self._apply_transforms(current, payload)
            self._write_existing(conn, current)

        _run_with_conn(_do, table=self._table)
        return self

    def _merge_with_transforms(self, payload: Dict[str, Any]) -> "DocumentReference":
        """set(merge=True) with transform fields: plain keys merged, transforms applied."""
        plain = {
            k: v
            for k, v in payload.items()
            if not isinstance(v, (Increment, ArrayUnion, ArrayRemove))
            and not isinstance(v, type(DELETE_FIELD))
            and not _is_real_delete_field(v)
        }

        def _do(conn: Any) -> None:
            row = self._read_row_for_update(conn)
            current = decode_stored_document(row[0] or {}) if row else {}
            if _has_dotted_key(plain):
                for k, v in plain.items():
                    _set_path(current, k, v)
            else:
                current.update(plain)
            self._apply_transforms(current, payload)
            if row is None:
                conn.execute(
                    text(upsert_sql(self._table)),
                    {"uid": self._uid or "", "doc_id": self._id, "data": document_dumps(current)},
                )
            else:
                self._write_existing(conn, current)

        _run_with_conn(_do, table=self._table)
        return self

    def delete(self, option: Any = None, **kwargs: Any) -> "DocumentReference":
        def _do(conn: Any) -> None:
            expected = _expected_update_time(option)
            if expected is None:
                conn.execute(text(delete_sql(self._table)), {"uid": self._uid or "", "doc_id": self._id})
                return
            result = conn.execute(
                text(delete_sql(self._table) + " AND updated_at = :expected"),
                {"uid": self._uid or "", "doc_id": self._id, "expected": expected},
            )
            if result.rowcount != 1:
                raise _api_exceptions.FailedPrecondition("document update_time precondition failed")

        _run_with_conn(_do, table=self._table)
        return self

    def create(self, document_data: Mapping[str, Any]) -> "DocumentReference":
        """create() fails if the document exists (Firestore semantics)."""
        payload, _ = _write_transform(document_data)
        materialized: Dict[str, Any] = {}
        self._apply_transforms(materialized, payload)
        payload = materialized
        payload = _strip_sentinels(payload)

        def _do(conn: Any) -> None:
            result = conn.execute(
                text(
                    f"INSERT INTO {self._table} (uid, doc_id, data, created_at, updated_at, version) "
                    "VALUES (:uid, :doc_id, CAST(:data AS jsonb), now(), clock_timestamp(), 1) "
                    "ON CONFLICT (uid, doc_id) DO NOTHING"
                ),
                {"uid": self._uid or "", "doc_id": self._id, "data": document_dumps(payload)},
            )
            if result.rowcount != 1:
                raise _api_exceptions.AlreadyExists(f"Document {self.path} already exists")

        _run_with_conn(_do, table=self._table)
        return self


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


class Transaction:
    """Transaction object; operations run on the thread-local PG connection."""

    def __init__(self, client: "Client", max_attempts: int = 5):
        self._client = client
        self._id = uuid.uuid4().hex
        self._read_only = False
        self._max_attempts = max_attempts
        self._conn = None
        self._conn_ctx = None

    # ------------------------------------------------------------------
    # Bridge for the REAL google-cloud-firestore ``transactional`` decorator
    # (business modules imported before compat.install bind it).
    # ------------------------------------------------------------------

    def _clean_up(self) -> None:
        self._rollback()
        self._id = uuid.uuid4().hex

    def _begin(self, retry_id: Optional[str] = None) -> None:
        """Start the PG transaction and install it as the thread-local conn."""
        if self._conn is not None:
            self._rollback()
        ctx = get_engine().begin()
        conn = ctx.__enter__()
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
        self._conn_ctx = ctx
        self._conn = conn
        self._id = uuid.uuid4().hex
        set_tx_conn(conn)

    def _commit(self) -> None:
        exc_info = None
        try:
            self._conn.commit()
        except Exception as exc:
            if _is_conflict_error(exc):
                aborted = _api_exceptions.Aborted(f"transaction conflict: {exc}")
                exc_info = (type(aborted), aborted, aborted.__traceback__)
            else:
                exc_info = (type(exc), exc, exc.__traceback__)
        finally:
            clear_tx_conn()
            if self._conn_ctx is not None:
                self._conn_ctx.__exit__(*exc_info) if exc_info else self._conn_ctx.__exit__(None, None, None)
                self._conn_ctx = None
            self._conn = None
        if exc_info is not None:
            raise exc_info[1]

    def _rollback(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.rollback()
        finally:
            clear_tx_conn()
            if self._conn_ctx is not None:
                self._conn_ctx.__exit__(None, None, None)
                self._conn_ctx = None
            self._conn = None

    def get(self, ref: Union[DocumentReference, Query], field_paths: Optional[List[str]] = None) -> Any:
        if isinstance(ref, DocumentReference):
            row = _run_with_conn(
                lambda conn: conn.execute(
                    text(get_sql(ref._table)), {"uid": ref._uid or "", "doc_id": ref._id}
                ).fetchone(),
                table=ref._table,
            )
            if row is None:
                return DocumentSnapshot(ref, exists=False, data=None)
            data, update_time, _version = row
            if field_paths is not None:
                data = {k: data[k] for k in field_paths if k in data}
            return DocumentSnapshot(ref, exists=True, data=data, update_time=update_time)
        return ref.get(transaction=self)

    def set(self, ref: DocumentReference, document_data: Mapping[str, Any], merge: bool = False) -> None:
        # delegate: set(merge=True) with transforms needs read-modify-write,
        # which _run_with_conn keeps on the tx connection inside a transaction
        ref.set(document_data, merge=merge)

    def update(self, ref: DocumentReference, field_updates: Mapping[str, Any], option: Any = None) -> None:
        ref.update(field_updates, option=option)

    def delete(self, ref: DocumentReference, option: Any = None) -> None:
        ref.delete(option=option)

    def create(self, ref: DocumentReference, document_data: Mapping[str, Any]) -> None:
        ref.create(document_data)


# ---------------------------------------------------------------------------
# Client + transactional decorator
# ---------------------------------------------------------------------------


class WriteBatch:
    """Firestore-compatible write batch: accumulate set/update/delete then commit.

    All operations execute in one SQLAlchemy transaction on ``commit()``
    (atomic), matching Firestore batch semantics.
    """

    def __init__(self, client: "Client") -> None:
        self._client = client
        self._ops: List[Tuple[str, Any]] = []

    def set(self, ref: DocumentReference, document_data: Mapping[str, Any], merge: bool = False) -> None:
        self._ops.append(("set", ref, dict(document_data), merge))

    def update(self, ref: DocumentReference, field_updates: Mapping[str, Any], option: Any = None) -> None:
        self._ops.append(("update", ref, dict(field_updates), option))

    def delete(self, ref: DocumentReference, option: Any = None) -> None:
        self._ops.append(("delete", ref, option))

    def create(self, ref: DocumentReference, document_data: Mapping[str, Any]) -> None:
        self._ops.append(("create", ref, dict(document_data)))

    def commit(self) -> None:
        for op in self._ops:
            require_table(op[1]._table)

        def _execute() -> None:
            for op in self._ops:
                kind = op[0]
                if kind == "set":
                    _, ref, data, merge = op
                    ref.set(data, merge=merge)
                elif kind == "update":
                    _, ref, data, option = op
                    ref.update(data, option=option)
                elif kind == "delete":
                    _, ref, option = op
                    ref.delete(option=option)
                elif kind == "create":
                    _, ref, data = op
                    ref.create(data)

        existing_conn = get_tx_conn()
        try:
            if existing_conn is not None:
                _execute()
            else:
                with get_engine().begin() as conn:
                    set_tx_conn(conn)
                    try:
                        _execute()
                    finally:
                        clear_tx_conn()
        finally:
            self._ops.clear()


class Client:
    """Drop-in for ``google.cloud.firestore.Client``."""

    def __init__(self, project: Optional[str] = None, **kwargs: Any):
        self._project = project
        if kwargs.pop("_verify_schema", True):
            check_schema()

    @property
    def project(self) -> Optional[str]:
        return self._project

    def batch(self) -> WriteBatch:
        return WriteBatch(self)

    def collection(self, collection_path: str) -> CollectionReference:
        return CollectionReference(collection_path, client=self)

    def collections(self) -> List[CollectionReference]:
        """Return top-level collections that currently contain documents.

        Firestore exposes this inventory on ``Client``.  Account privacy
        cleanup uses it to find UID-owned rows that live outside
        ``users/{uid}`` (for example durable jobs and API credentials), so the
        PostgreSQL shim must not limit enumeration to a hard-coded table list.
        Tables used only as nested subcollections are excluded by requiring a
        top-level parent namespace (``uid = ''``).
        """

        with get_engine().begin() as conn:
            rows = conn.execute(
                text(f"SELECT collection_id, table_name FROM {COLLECTION_TABLE} ORDER BY collection_id")
            ).fetchall()
            candidates = [(str(collection_id), str(table)) for collection_id, table in rows]
            if candidates:
                params: dict[str, Any] = {}
                probes = []
                for index, (collection_id, table) in enumerate(candidates):
                    params[f'collection_id_{index}'] = collection_id
                    probes.append(
                        f"(SELECT :collection_id_{index} AS collection_id FROM {table} WHERE uid = '' LIMIT 1)"
                    )
                tables = [row[0] for row in conn.execute(text(' UNION ALL '.join(probes)), params).fetchall()]
            else:  # pragma: no cover - a migrated schema always registers known tables
                tables = []
        return [CollectionReference(table, client=self) for table in sorted(tables)]

    def document(self, document_path: str) -> DocumentReference:
        parts = [part for part in document_path.split("/") if part]
        if not parts or len(parts) % 2 != 0:
            raise ValueError(f"invalid Firestore document path: {document_path!r}")
        collection_path = "/".join(parts[:-1])
        parent = CollectionReference(collection_path, client=self)
        return DocumentReference(
            parent._table,
            parent._uid,
            parts[-1],
            parent=parent,
            path="/".join(parts),
            collection_id=parent.id,
        )

    def collection_group(self, collection_name: str) -> Query:
        parent = CollectionReference(collection_name, client=self)
        return Query(
            table=collection_table_name(collection_name),
            collection_id=collection_name,
            uid=None,
            parent=parent,
            collection_group=True,
        )

    def transaction(self) -> Transaction:
        return Transaction(self)

    def get_all(self, refs: Iterable[DocumentReference], **kwargs: Any) -> List[DocumentSnapshot]:
        return [ref.get() for ref in refs]

    def write_option(self, *, last_update_time: datetime) -> LastUpdateOption:
        return LastUpdateOption(last_update_time)


def transactional(func: Callable[..., Any]) -> Callable[..., Any]:
    """Drop-in for ``@firestore.transactional``.

    Two transaction kinds are accepted, matching the real SDK:
    - a firestore_pg ``Transaction`` -> run on a PG SERIALIZABLE connection
      (thread-local tx conn installed so body reads/writes share it), retrying
      serialization conflicts.
    - any duck-typed object exposing the SDK lifecycle hooks
      (``_begin`` / ``_commit`` / ``_rollback`` / ``_max_attempts`` /
      ``_read_only``) — e.g. test fakes injected via DI — handled exactly like
      the real ``_Transactional`` (no PG connection involved).
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Locate the Transaction argument (positional or keyword).
        tx = kwargs.get("transaction")
        if tx is None:
            for arg in args:
                if isinstance(arg, Transaction) or _is_duck_transaction(arg):
                    tx = arg
                    break
        if tx is None:
            raise TypeError("transactional() requires a Transaction argument")

        if not isinstance(tx, Transaction):
            return _run_duck_transaction(tx, func, args, kwargs)

        engine = get_engine()
        attempts = tx._max_attempts
        last_exc: Optional[_api_exceptions.Aborted] = None
        for attempt in range(attempts):
            try:
                with engine.begin() as conn:
                    conn.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                    set_tx_conn(conn)
                    try:
                        return func(*args, **kwargs)
                    finally:
                        clear_tx_conn()
            except Exception as exc:
                clear_tx_conn()
                if _is_conflict_error(exc):
                    last_exc = (
                        exc
                        if isinstance(exc, _api_exceptions.Aborted)
                        else _api_exceptions.Aborted(f'transaction conflict: {exc}')
                    )
                    if attempt + 1 < attempts:
                        logger.warning("firestore_pg: transaction conflict, retry %d/%d", attempt + 2, attempts)
                        continue
                    break
                raise
        raise ValueError(f'Failed to commit transaction in {attempts:d} attempts.') from last_exc

    return wrapper


def _is_duck_transaction(arg: Any) -> bool:
    """True if arg exposes the SDK transaction lifecycle hooks (test fakes)."""
    return bool(
        arg is not None
        and not isinstance(arg, Transaction)
        and all(hasattr(arg, name) for name in ("_begin", "_commit", "_rollback", "_max_attempts"))
    )


def _run_duck_transaction(tx: Any, func: Callable[..., Any], args: tuple, kwargs: dict) -> Any:
    """Run ``func`` against a duck-typed transaction, mirroring the real SDK's
    ``_Transactional.__call__``: begin, run (retrying Aborted), commit, and
    rollback on any error. No PG connection is used — the fake owns its state.
    """
    try:
        last_exc: Optional[Exception] = None
        for attempt in range(tx._max_attempts):
            if attempt == 0 or last_exc is not None:
                tx._begin(retry_id=getattr(tx, "_id", None))
            try:
                result = func(*args, **kwargs)
                tx._commit()
                return result
            except _api_exceptions.Aborted as exc:
                last_exc = exc
        raise ValueError(f"transaction failed after {tx._max_attempts} attempts") from last_exc
    except BaseException:
        tx._rollback()
        raise


# module-level alias for ``from google.cloud import firestore`` style usage
transactional = transactional
