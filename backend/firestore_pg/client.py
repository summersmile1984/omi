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
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

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
from .sql import delete_sql, get_sql, json_dumps, list_sql, merge_sql, resolve_collection, upsert_sql

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


class DocumentSnapshot:
    def __init__(self, reference: "DocumentReference", exists: bool, data: Optional[Mapping[str, Any]], update_time: Optional[datetime] = None):
        self._reference = reference
        self._exists = exists
        self._data = _coerce_value_for_read(dict(data)) if data else {}
        self.update_time = update_time or datetime.now(timezone.utc)

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
        return dict(self._data) if self._data else None

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
    return {
        k: v
        for k, v in payload.items()
        if not (isinstance(v, type(DELETE_FIELD)) or _is_real_delete_field(v))
    }


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------


def _build_query_sql(
    table: str,
    *,
    uid: Optional[str],
    filters: List[FieldFilter],
    order_bys: List[Tuple[str, str]],
    limit: Optional[int],
    collection_group: bool = False,
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
        if f.op_string in ("array-contains",):
            where.append(f"{_json_path(f.field_path)} @> CAST({param} AS jsonb)")
            params[pname] = json_dumps([f.value])
        elif f.op_string == "array-contains-any":
            where.append(f"{_json_path(f.field_path)} ?| CAST({param} AS text[])")
            params[pname] = [str(v) for v in f.value]
        elif f.op_string == "in":
            where.append(f"{_text_path(f.field_path)} IN (SELECT unnest(CAST({param} AS text[])))")
            params[pname] = [str(v) for v in f.value]
        elif f.op_string == "not-in":
            where.append(f"({_text_path(f.field_path)} IS NULL OR {_text_path(f.field_path)} NOT IN (SELECT unnest(CAST({param} AS text[]))))")
            params[pname] = [str(v) for v in f.value]
        else:
            lhs, cast = _comparison_lhs(f.field_path, f.value)
            where.append(f"{lhs} {_OPERATORS_SQL[f.op_string]} {param}")
            params[pname] = f.value
            if cast is not None:
                # CAST(:param AS <type>) keeps psycopg pyformat happy (no
                # ``::type`` suffix, which would swallow the colon as a param).
                where[-1] = f"{lhs} {_OPERATORS_SQL[f.op_string]} CAST({param} AS {cast})"
            else:
                where[-1] = f"{lhs} {_OPERATORS_SQL[f.op_string]} {param}"

    order_sql = ""
    if order_bys:
        clauses = []
        for field, direction in order_bys:
            # Firestore sorts docs missing the field LAST, regardless of
            # direction; PG defaults to NULLS FIRST on DESC, so pin NULLS LAST.
            clauses.append(f"{_text_path(field)} {'ASC' if direction == 'ASCENDING' else 'DESC'} NULLS LAST")
        order_sql = " ORDER BY " + ", ".join(clauses)

    limit_sql = f" LIMIT {int(limit)}" if limit is not None else ""

    sql = f"SELECT doc_id, data FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += order_sql + limit_sql
    return sql, params


def _is_conflict_error(exc: Exception) -> bool:
    """True for PG serialization failures / deadlocks (retryable)."""
    msg = str(exc).lower()
    return "serializ" in msg or "deadlock" in msg


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
        collection_group: bool = False,
    ):
        self._table = table
        self._uid = uid
        self._parent = parent
        self._filters = list(filters or [])
        self._order_bys = list(order_bys or [])
        self._limit = limit
        self._collection_group = collection_group

    def where(self, *args: Any, filter: Any = None, **kwargs: Any) -> "Query":
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
            collection_group=self._collection_group,
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
            collection_group=self._collection_group,
        )

    def limit(self, count: int) -> "Query":
        return Query(
            table=self._table,
            uid=self._uid,
            parent=self._parent,
            filters=self._filters,
            order_bys=self._order_bys,
            limit=count,
            collection_group=self._collection_group,
        )

    def stream(self, transaction: Optional["Transaction"] = None) -> Iterator[DocumentSnapshot]:
        if transaction is not None:
            return self._run(transaction=transaction)
        return self._run()

    def get(self, transaction: Optional["Transaction"] = None) -> QuerySnapshot:
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
            collection_group=self._collection_group,
        )
        ensure_table(self._table)
        if tx_conn is not None:
            rows = tx_conn.execute(text(sql), params).fetchall()
        else:
            with engine.begin() as conn:
                rows = conn.execute(text(sql), params).fetchall()
        for row in rows:
            doc_id, raw = row[0], row[1]
            yield DocumentSnapshot(
                DocumentReference(self._table, self._uid, doc_id, parent=self._parent),
                exists=True,
                data=raw,
            )


class CollectionReference:
    def __init__(self, path: str, client: Optional["Client"] = None, table: Optional[str] = None, uid: Optional[str] = None):
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

    def add(self, document_data: Mapping[str, Any]) -> Tuple["DocumentReference", Any]:
        ref = self.document()
        ref.set(document_data)
        return ref, ref.get()

    def stream(self) -> Iterator[DocumentSnapshot]:
        return self._query().stream()

    def get(self) -> QuerySnapshot:
        return self._query().get()

    def where(self, *args: Any, filter: Any = None, **kwargs: Any) -> Query:
        return self._query().where(*args, filter=filter, **kwargs)

    def order_by(self, field_path: str, direction: Optional[str] = None) -> Query:
        return self._query().order_by(field_path, direction)

    def limit(self, count: int) -> Query:
        return self._query().limit(count)

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
        base = f"users/{self._uid}" if self._uid else self._table
        return f"{base}/{self._table}/{self._id}"

    def collection(self, collection_path: str) -> CollectionReference:
        # children of users/{uid}/<coll>/{doc} live at a path that
        # resolve_collection maps back to a (table, uid) namespace
        return CollectionReference(f"{self.path}/{collection_path}")

    def get(self, transaction: Optional["Transaction"] = None) -> DocumentSnapshot:
        if transaction is not None:
            return transaction.get(self)
        row = _run_with_conn(
            lambda conn: conn.execute(
                text(get_sql(self._table)), {"uid": self._uid or "", "doc_id": self._id}
            ).fetchone(),
            table=self._table,
        )
        if row is None:
            return DocumentSnapshot(self, exists=False, data=None)
        return DocumentSnapshot(self, exists=True, data=row[0])

    def set(self, document_data: Mapping[str, Any], merge: bool = False) -> "DocumentReference":
        payload, has_transform = _write_transform(document_data)
        if has_transform and merge:
            # merge=True with transforms (e.g. record_user_platform): merge the
            # plain fields, then apply transforms against the merged doc
            return self._merge_with_transforms(payload)
        payload = _strip_sentinels(payload)
        sql = merge_sql(self._table) if merge else upsert_sql(self._table)
        _run_with_conn(
            lambda conn: conn.execute(
                text(sql), {"uid": self._uid or "", "doc_id": self._id, "data": json_dumps(payload)}
            ),
            table=self._table,
        )
        return self

    def update(self, field_updates: Mapping[str, Any]) -> "DocumentReference":
        payload, has_transform = _write_transform(field_updates)
        if has_transform:
            # read-modify-write under the transaction's lock (or a short lock)
            return self._update_with_transforms(payload)

        def _do(conn: Any) -> None:
            row = conn.execute(
                text(get_sql(self._table)), {"uid": self._uid or "", "doc_id": self._id}
            ).fetchone()
            if row is None:
                raise ValueError(f"Document {self.path} does not exist")
            merged = dict(row[0] or {})
            if _has_dotted_key(payload):
                for k, v in payload.items():
                    _set_path(merged, k, v)
            else:
                merged.update(payload)
            conn.execute(
                text(upsert_sql(self._table)),
                {"uid": self._uid or "", "doc_id": self._id, "data": json_dumps(merged)},
            )

        _run_with_conn(_do, table=self._table)
        return self

    @staticmethod
    def _apply_transforms(current: Dict[str, Any], payload: Dict[str, Any]) -> None:
        for key, value in payload.items():
            dotted = "." in str(key)
            if isinstance(value, Increment):
                cur = _get_path(current, key) if dotted else current.get(key)
                _set_path(current, key, int(cur or 0) + int(value.value))
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

    def _read_current(self, conn: Any) -> Dict[str, Any]:
        row = conn.execute(
            text(get_sql(self._table)), {"uid": self._uid or "", "doc_id": self._id}
        ).fetchone()
        return dict(row[0] or {}) if row else {}

    def _write_current(self, conn: Any, current: Dict[str, Any]) -> None:
        conn.execute(
            text(upsert_sql(self._table)),
            {"uid": self._uid or "", "doc_id": self._id, "data": json_dumps(current)},
        )

    def _update_with_transforms(self, payload: Dict[str, Any]) -> "DocumentReference":
        def _do(conn: Any) -> None:
            current = self._read_current(conn)
            self._apply_transforms(current, payload)
            self._write_current(conn, current)

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
            current = self._read_current(conn)
            if _has_dotted_key(plain):
                for k, v in plain.items():
                    _set_path(current, k, v)
            else:
                current.update(plain)
            self._apply_transforms(current, payload)
            self._write_current(conn, current)

        _run_with_conn(_do, table=self._table)
        return self

    def delete(self) -> "DocumentReference":
        _run_with_conn(
            lambda conn: conn.execute(
                text(delete_sql(self._table)), {"uid": self._uid or "", "doc_id": self._id}
            ),
            table=self._table,
        )
        return self

    def create(self, document_data: Mapping[str, Any]) -> "DocumentReference":
        """create() fails if the document exists (Firestore semantics)."""
        payload, _ = _write_transform(document_data)
        payload = _strip_sentinels(payload)

        def _do(conn: Any) -> None:
            row = conn.execute(
                text(get_sql(self._table)), {"uid": self._uid or "", "doc_id": self._id}
            ).fetchone()
            if row is not None:
                raise _api_exceptions.AlreadyExists(f"Document {self.path} already exists")
            conn.execute(
                text(upsert_sql(self._table)),
                {"uid": self._uid or "", "doc_id": self._id, "data": json_dumps(payload)},
            )

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

    def get(self, ref: Union[DocumentReference, Query]) -> Any:
        if isinstance(ref, DocumentReference):
            row = _run_with_conn(
                lambda conn: conn.execute(
                    text(get_sql(ref._table)), {"uid": ref._uid or "", "doc_id": ref._id}
                ).fetchone(),
                table=ref._table,
            )
            if row is None:
                return DocumentSnapshot(ref, exists=False, data=None)
            return DocumentSnapshot(ref, exists=True, data=row[0])
        return ref.get(transaction=self)

    def set(self, ref: DocumentReference, document_data: Mapping[str, Any], merge: bool = False) -> None:
        # delegate: set(merge=True) with transforms needs read-modify-write,
        # which _run_with_conn keeps on the tx connection inside a transaction
        ref.set(document_data, merge=merge)

    def update(self, ref: DocumentReference, field_updates: Mapping[str, Any]) -> None:
        ref.update(field_updates)

    def delete(self, ref: DocumentReference) -> None:
        _run_with_conn(
            lambda conn: conn.execute(
                text(delete_sql(ref._table)), {"uid": ref._uid or "", "doc_id": ref._id}
            ),
            table=ref._table,
        )


# ---------------------------------------------------------------------------
# Client + transactional decorator
# ---------------------------------------------------------------------------


class Client:
    """Drop-in for ``google.cloud.firestore.Client``."""

    def __init__(self, project: Optional[str] = None, **kwargs: Any):
        self._project = project
        if kwargs.pop("_ensure_schema", True):
            try:
                ensure_tables()
                ensure_composite_indexes()
            except Exception as exc:  # pragma: no cover - schema failure is fatal in dev
                logger.warning("firestore_pg: schema ensure failed (continuing): %s", exc)

    @property
    def project(self) -> Optional[str]:
        return self._project

    def collection(self, collection_path: str) -> CollectionReference:
        return CollectionReference(collection_path, client=self)

    def collection_group(self, collection_name: str) -> Query:
        return Query(table=collection_name, uid=None, collection_group=True)

    def transaction(self) -> Transaction:
        return Transaction(self)

    def get_all(self, refs: Iterable[DocumentReference]) -> List[DocumentSnapshot]:
        return [ref.get() for ref in refs]


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
