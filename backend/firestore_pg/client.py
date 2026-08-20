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

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from sqlalchemy import text

from google.api_core import exceptions as _api_exceptions

from . import (
    ArrayRemove,
    ArrayUnion,
    DELETE_FIELD,
    FieldFilter,
    Increment,
    SERVER_TIMESTAMP,
    _ensure_filter,
    _is_real_delete_field,
    _normalize_transform,
    _coerce_value_for_read,
)
from .engine import (
    _clear_tx_conn,
    _get_tx_conn,
    _set_tx_conn,
    ensure_composite_indexes,
    ensure_table,
    ensure_tables,
    get_engine,
)
from .sql import delete_sql, get_sql, json_dumps, merge_sql, resolve_collection, upsert_sql

logger = logging.getLogger(__name__)

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
        self._data = _coerce_value_for_read(dict(data)) if data else {}
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
            payload[key] = datetime.now(timezone.utc).isoformat()
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


def _build_query_sql(
    table: str,
    *,
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
    where: List[str] = []
    params: Dict[str, Any] = {}
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
            lhs = _document_name_expr(table) if collection_group else "doc_id"
            if not collection_group and hasattr(f.value, "id"):
                fv = f.value.id
        elif lhs is None:
            lhs = _text_path(f.field_path)
        if op in ("array-contains", "array_contains"):
            where.append(f"{_json_path(f.field_path)} @> CAST({param} AS jsonb)")
            params[pname] = json_dumps([f.value])
        elif op in ("array-contains-any", "array_contains_any"):
            where.append(
                f"EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof({_json_path(f.field_path)}) = 'array' "
                f"THEN {_json_path(f.field_path)} ELSE '[]'::jsonb END) AS actual "
                f"WHERE actual IN (SELECT jsonb_array_elements(CAST({param} AS jsonb))))"
            )
            params[pname] = json_dumps(list(f.value))
        elif op == "in":
            if f.field_path == "__name__":
                values = [(v.path if collection_group else v.id) if hasattr(v, "path") else str(v) for v in f.value]
                where.append(f"{lhs} IN (SELECT unnest(CAST({param} AS text[])))")
                params[pname] = values
            else:
                where.append(f"{_json_path(f.field_path)} IN (SELECT jsonb_array_elements(CAST({param} AS jsonb)))")
                params[pname] = json_dumps(list(f.value))
        elif op == "not-in":
            # Firestore excludes documents where the field is missing or null.
            if any(value is None for value in f.value):
                where.append("FALSE")
            elif f.field_path == "__name__":
                values = [(v.path if collection_group else v.id) if hasattr(v, "path") else str(v) for v in f.value]
                where.append(f"{lhs} NOT IN (SELECT unnest(CAST({param} AS text[])))")
                params[pname] = values
            else:
                where.append(
                    f"{_json_path(f.field_path)} IS NOT NULL AND {_json_path(f.field_path)} <> 'null'::jsonb "
                    f"AND {_json_path(f.field_path)} NOT IN (SELECT jsonb_array_elements(CAST({param} AS jsonb)))"
                )
                params[pname] = json_dumps(list(f.value))
        elif op in ("==", "!=") and f.field_path != "__name__":
            operator = "=" if op == "==" else "<>"
            where.append(f"{_json_path(f.field_path)} {operator} CAST({param} AS jsonb)")
            params[pname] = json_dumps(fv)
        else:
            cast_lhs, cast = _comparison_lhs(f.field_path, fv)
            if f.field_path == "__name__":
                cast_lhs, cast = (_document_name_expr(table) if collection_group else "doc_id"), None
            where.append(f"{cast_lhs} {_OPERATORS_SQL[op]} {param}")
            params[pname] = fv
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
        cursor_terms: List[str] = []
        for idx, ((field, direction), value) in enumerate(zip(cursor_order_bys, cursor_values)):
            prefix: List[str] = []
            for prior_idx in range(idx):
                prior_field = cursor_order_bys[prior_idx][0]
                prior_expr = (
                    (_document_name_expr(table) if collection_group else "doc_id")
                    if prior_field == "__name__"
                    else _json_path(prior_field)
                )
                prior_param = f":cursor_{prior_idx}"
                prefix.append(
                    f"{prior_expr} = {prior_param}"
                    if prior_field == "__name__"
                    else f"{prior_expr} = CAST({prior_param} AS jsonb)"
                )
            expr = (
                (_document_name_expr(table) if collection_group else "doc_id")
                if field == "__name__"
                else _json_path(field)
            )
            cursor_param = f":cursor_{idx}"
            comparator = ">" if direction == "ASCENDING" else "<"
            comparison = (
                f"{expr} {comparator} {cursor_param}"
                if field == "__name__"
                else f"{expr} {comparator} CAST({cursor_param} AS jsonb)"
            )
            cursor_terms.append("(" + " AND ".join(prefix + [comparison]) + ")")
            params[f"cursor_{idx}"] = value if field == "__name__" else json_dumps(value)
        where.append("(" + " OR ".join(cursor_terms) + ")")

    order_sql = ""
    if effective_order_bys:
        clauses = []
        for field, direction in effective_order_bys:
            if field == "__name__":
                name_expr = _document_name_expr(table) if collection_group else "doc_id"
                clauses.append(f"{name_expr} {'ASC' if direction == 'ASCENDING' else 'DESC'}")
            else:
                # PostgreSQL JSONB ordering keeps numbers numeric (rather than
                # lexicographically sorting their text representation).
                clauses.append(f"{_json_path(field)} {'ASC' if direction == 'ASCENDING' else 'DESC'}")
        order_sql = " ORDER BY " + ", ".join(clauses)

    limit_sql = f" LIMIT {int(limit)}" if limit is not None else ""
    offset_sql = f" OFFSET {int(offset)}" if offset is not None else ""

    sql = f"SELECT uid, doc_id, data, updated_at, version FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += order_sql + limit_sql + offset_sql
    return sql, params


def _document_name_expr(table: str) -> str:
    """SQL expression for Firestore's fully-qualified ``__name__`` path."""
    return f"(CASE WHEN uid = '' THEN '{table}/' || doc_id ELSE uid || '/{table}/' || doc_id END)"


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
        ensure_table(table)
    engine = get_engine()
    tx_conn = _get_tx_conn()
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
    if "." not in field_path:
        return f"data->>'{field_path}'"
    segments = ",".join(field_path.split("."))
    return f"data #>> '{{{segments}}}'"


def _json_path(field_path: str) -> str:
    """SQL jsonb (non-text) expression for a path, for array/containment ops."""
    if "." not in field_path:
        return f"data->'{field_path}'"
    segments = ",".join(field_path.split("."))
    return f"data #> '{{{segments}}}'"


def _comparison_lhs(field_path: str, value: Any) -> Tuple[str, Optional[str]]:
    """SQL lhs for a scalar comparison and the CAST for the parameter.

    JSONB ``data->>'field'`` is text; comparing it against a timestamp,
    numeric, or boolean parameter needs matching casts on both sides. ISO-8601
    strings would compare lexicographically, which is only correct for
    timestamps in one timezone, so cast to the native type when known.
    """
    if isinstance(value, datetime):
        return f"CAST({_text_path(field_path)} AS timestamptz)", "timestamptz"
    if isinstance(value, bool):
        return f"CAST({_text_path(field_path)} AS boolean)", "boolean"
    if isinstance(value, (int, float)):
        return f"CAST({_text_path(field_path)} AS double precision)", "double precision"
    return _text_path(field_path), None


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
        from . import BaseCompositeFilter

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
        direction = direction or "ASCENDING"
        return Query(
            table=self._table,
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
        return Query(
            table=self._table,
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
            tx_conn = _get_tx_conn()
        sql, params = _build_query_sql(
            self._table,
            uid=self._uid,
            filters=self._filters,
            order_bys=self._order_bys,
            limit=self._limit,
            offset=self._offset,
            projection=self._projection,
            collection_group=self._collection_group,
            cursor=self._cursor,
        )
        ensure_table(self._table)
        if tx_conn is not None:
            rows = tx_conn.execute(text(sql), params).fetchall()
        else:
            with engine.begin() as conn:
                rows = conn.execute(text(sql), params).fetchall()
        for row in rows:
            namespace, doc_id, raw, update_time, _version = row
            if self._projection is not None:
                raw = {k: raw[k] for k in self._projection if k in raw}
            collection_path = f"{namespace}/{self._table}" if namespace else self._table
            parent = (
                self._parent
                if not self._collection_group
                else CollectionReference(collection_path, table=self._table, uid=namespace)
            )
            yield DocumentSnapshot(
                DocumentReference(self._table, namespace, doc_id, parent=parent),
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
            uid=self._query._uid,
            filters=self._query._filters,
            order_bys=self._query._order_bys,
            limit=self._query._limit,
            offset=self._query._offset,
            collection_group=self._query._collection_group,
            cursor=self._query._cursor,
        )
        count_sql = f"SELECT count(*) FROM ({sql}) AS _agg"
        ensure_table(self._query._table)
        tx_conn = _get_tx_conn()
        if tx_conn is not None:
            total = tx_conn.execute(text(count_sql), params).fetchone()[0]
        else:
            with get_engine().begin() as conn:
                total = conn.execute(text(count_sql), params).fetchone()[0]
        return [[_AggregationResult(int(total))]]


class CollectionReference:
    def __init__(
        self, path: str, client: Optional["Client"] = None, table: Optional[str] = None, uid: Optional[str] = None
    ):
        self._path = path
        self._client = client
        if table is not None:
            self._table, self._uid = table, uid
        else:
            self._table, self._uid = resolve_collection(path)

    @property
    def id(self) -> str:
        return self._table

    @property
    def path(self) -> str:
        return self._path

    def document(self, document_id: Optional[str] = None) -> "DocumentReference":
        if document_id is None:
            document_id = uuid.uuid4().hex
        return DocumentReference(self._table, self._uid, document_id, parent=self)

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
        return Query(table=self._table, uid=self._uid, parent=self)


class DocumentReference:
    def __init__(
        self,
        table: str,
        uid: Optional[str],
        document_id: str,
        parent: Optional[CollectionReference] = None,
        path: Optional[str] = None,
    ):
        self._table = table
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
        collection_path = f"{self._uid}/{self._table}" if self._uid else self._table
        return f"{collection_path}/{self._id}"

    @property
    def parent(self) -> Optional[CollectionReference]:
        return self._parent

    def collection(self, collection_path: str) -> CollectionReference:
        # children of users/{uid}/<coll>/{doc} live at a path that
        # resolve_collection maps back to a (table, uid) namespace
        return CollectionReference(f"{self.path}/{collection_path}")

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
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND column_name IN "
                    "('uid', 'doc_id', 'data', 'updated_at', 'version') "
                    "GROUP BY table_name HAVING count(DISTINCT column_name) = 5"
                )
            ).fetchall()
            candidates = [table for (table,) in rows]
            if candidates:
                # One round trip with indexed existence probes, rather than a
                # query per table for every document visited by recursive
                # account deletion.
                union = " UNION ALL ".join(
                    f"(SELECT '{table}' AS table_name FROM {table} WHERE uid = :path LIMIT 1)" for table in candidates
                )
                tables = [row[0] for row in conn.execute(text(union), {"path": parent_path}).fetchall()]
            else:  # pragma: no cover - Client bootstrap always creates tables
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
                text(sql), {"uid": self._uid or "", "doc_id": self._id, "data": json_dumps(payload)}
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
            merged = dict(row[0] or {})
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
                    if item not in arr:
                        arr.append(item)
                _set_path(current, key, arr)
            elif isinstance(value, ArrayRemove):
                cur = _get_path(current, key) if dotted else current.get(key)
                arr = list(cur or [])
                _set_path(current, key, [i for i in arr if i not in value.value])
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
            {"uid": self._uid or "", "doc_id": self._id, "data": json_dumps(current)},
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
            current = dict(row[0] or {})
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
            current = dict(row[0] or {}) if row else {}
            if _has_dotted_key(plain):
                for k, v in plain.items():
                    _set_path(current, k, v)
            else:
                current.update(plain)
            self._apply_transforms(current, payload)
            if row is None:
                conn.execute(
                    text(upsert_sql(self._table)),
                    {"uid": self._uid or "", "doc_id": self._id, "data": json_dumps(current)},
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
                {"uid": self._uid or "", "doc_id": self._id, "data": json_dumps(payload)},
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
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        self._conn_ctx = ctx
        self._conn = conn
        self._id = uuid.uuid4().hex
        _set_tx_conn(conn)

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
            _clear_tx_conn()
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
            _clear_tx_conn()
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
            ensure_table(op[1]._table)

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

        existing_conn = _get_tx_conn()
        try:
            if existing_conn is not None:
                _execute()
            else:
                with get_engine().begin() as conn:
                    _set_tx_conn(conn)
                    try:
                        _execute()
                    finally:
                        _clear_tx_conn()
        finally:
            self._ops.clear()


class Client:
    """Drop-in for ``google.cloud.firestore.Client``."""

    def __init__(self, project: Optional[str] = None, **kwargs: Any):
        self._project = project
        if kwargs.pop("_ensure_schema", True):
            ensure_tables()
            ensure_composite_indexes()

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
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND column_name IN "
                    "('uid', 'doc_id', 'data', 'updated_at', 'version') "
                    "GROUP BY table_name HAVING count(DISTINCT column_name) = 5"
                )
            ).fetchall()
            candidates = [table for (table,) in rows]
            if candidates:
                union = " UNION ALL ".join(
                    f"(SELECT '{table}' AS table_name FROM {table} WHERE uid = '' LIMIT 1)" for table in candidates
                )
                tables = [row[0] for row in conn.execute(text(union)).fetchall()]
            else:  # pragma: no cover - Client bootstrap always creates tables
                tables = []
        return [CollectionReference(table, client=self) for table in sorted(tables)]

    def document(self, document_path: str) -> DocumentReference:
        parts = [part for part in document_path.split("/") if part]
        if not parts or len(parts) % 2 != 0:
            raise ValueError(f"invalid Firestore document path: {document_path!r}")
        collection_path = "/".join(parts[:-1])
        parent = CollectionReference(collection_path, client=self)
        return DocumentReference(parent._table, parent._uid, parts[-1], parent=parent, path="/".join(parts))

    def collection_group(self, collection_name: str) -> Query:
        return Query(table=collection_name, uid=None, collection_group=True)

    def transaction(self) -> Transaction:
        return Transaction(self)

    def get_all(self, refs: Iterable[DocumentReference], **kwargs: Any) -> List[DocumentSnapshot]:
        return [ref.get() for ref in refs]

    def write_option(self, *, last_update_time: datetime) -> LastUpdateOption:
        return LastUpdateOption(last_update_time)


def transactional(func: Callable[..., Any]) -> Callable[..., Any]:
    """Drop-in for ``@firestore.transactional``.

    Two transaction kinds are accepted, matching the real SDK:
    - a firestore_pg ``Transaction`` -> run on a PG REPEATABLE READ connection
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
        attempts = kwargs.pop("_retries", 3) or 3
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                with engine.begin() as conn:
                    conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                    _set_tx_conn(conn)
                    try:
                        return func(*args, **kwargs)
                    finally:
                        _clear_tx_conn()
            except Exception as exc:
                last_exc = exc
                _clear_tx_conn()
                if _is_conflict_error(exc):
                    if attempt + 1 < attempts:
                        logger.warning("firestore_pg: transaction conflict, retry %d/%d", attempt + 2, attempts)
                        continue
                raise
        raise last_exc  # pragma: no cover

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
    from google.api_core.exceptions import Aborted as _Aborted

    try:
        last_exc: Optional[Exception] = None
        for attempt in range(tx._max_attempts):
            if attempt == 0 or last_exc is not None:
                tx._begin(retry_id=getattr(tx, "_id", None))
            try:
                result = func(*args, **kwargs)
                tx._commit()
                return result
            except _Aborted as exc:
                last_exc = exc
        raise ValueError(f"transaction failed after {tx._max_attempts} attempts") from last_exc
    except BaseException:
        tx._rollback()
        raise


# module-level alias for ``from google.cloud import firestore`` style usage
transactional = transactional
