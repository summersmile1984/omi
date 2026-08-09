"""Shadow-diff scenarios: run the same Firestore API sequence against the real
SDK (emulator) and against the firestore_pg shim, then diff normalized results.

Each scenario is a callable taking a ``db`` client and returning a JSON-safe
normalized value. Normalization rules (``_norm``) keep cross-SDK diffs honest:

- ``datetime`` -> ISO-8601 string (shim returns datetime via coercion, real SDK
  returns a Timestamp with microsecond precision; truncate to millis for parity)
- dicts/lists recursed; tuples -> lists
- query snapshots -> sorted list of (doc_id, to_dict())
- sentinels (DELETE_FIELD / SERVER_TIMESTAMP) -> their repr marker

Scenarios avoid behavior that legitimately differs across backends (ordering of
``in`` results, server-generated ids, commit timestamps).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

Scenario = Callable[[Any], Any]

_EPOCH = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _iso(value: Any) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _norm(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"$datetime": _iso(value)}
    if hasattr(value, "to_dict"):
        d = value.to_dict() or {}
        return {k: _norm(v) for k, v in d.items()}
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    # google.protobuf wrappers / SDK sentinel markers
    if type(value).__name__ == "DELETE_FIELD":
        return {"$delete_field": True}
    if type(value).__name__ == "SERVER_TIMESTAMP":
        return {"$server_timestamp": True}
    if type(value).__name__ == "_Path":  # FieldPath
        return {"$field_path": list(value.parts) if hasattr(value, "parts") else str(value)}
    return value


def _snaps(snapshot_list: Any) -> List[Dict[str, Any]]:
    docs = [{"doc_id": d.id, "data": _norm(d.to_dict())} for d in snapshot_list]
    docs.sort(key=lambda d: (d["doc_id"], str(d["data"])))
    return docs


def _ref(db: Any, doc_id: str):
    return db.collection("shadow_docs").document(doc_id)


def scenario_set_get(db: Any) -> Any:
    _ref(db, "doc1").set({"name": "alice", "age": 30, "tags": ["a", "b"], "at": _EPOCH})
    snap = _ref(db, "doc1").get()
    return {"exists": snap.exists, "data": _norm(snap)}


def scenario_set_merge(db: Any) -> Any:
    _ref(db, "m1").set({"name": "bob", "age": 40})
    _ref(db, "m1").set({"age": 41, "city": "nyc"}, merge=True)
    return _norm(_ref(db, "m1").get())


def scenario_update_fields(db: Any) -> Any:
    _ref(db, "u1").set({"name": "carol", "age": 20, "active": True})
    _ref(db, "u1").update({"age": 21, "active": False, "note": "updated"})
    return _norm(_ref(db, "u1").get())


def scenario_delete(db: Any) -> Any:
    _ref(db, "d1").set({"x": 1})
    _ref(db, "d1").delete()
    return {"exists_after_delete": _ref(db, "d1").get().exists}


def scenario_query_eq(db: Any) -> Any:
    for i, n in enumerate(["alpha", "beta", "gamma"]):
        _ref(db, f"q{i}").set({"kind": n, "num": i})
    q = db.collection("shadow_docs").where("kind", "==", "beta")
    return _snaps(q.stream())


def scenario_query_comparison(db: Any) -> Any:
    for i in range(5):
        _ref(db, f"c{i}").set({"num": i})
    q = db.collection("shadow_docs").where("num", ">=", 2).where("num", "<", 4)
    return _snaps(q.stream())


def scenario_query_order_limit(db: Any) -> Any:
    for i, n in enumerate(["z", "a", "m"]):
        _ref(db, f"o{i}").set({"name": n, "rank": i})
    q = db.collection("shadow_docs").order_by("rank", direction="DESCENDING").limit(2)
    return _snaps(q.stream())


def scenario_query_in(db: Any) -> Any:
    for i in range(4):
        _ref(db, f"i{i}").set({"code": f"c{i}"})
    q = db.collection("shadow_docs").where("code", "in", ["c0", "c2", "c9"])
    return _snaps(q.stream())


def scenario_array_union_remove(db: Any) -> Any:
    from google.cloud.firestore import ArrayRemove, ArrayUnion

    _ref(db, "arr").set({"tags": ["x", "y"]})
    _ref(db, "arr").update({"tags": ArrayUnion(["y", "z"])})
    _ref(db, "arr").update({"tags": ArrayRemove(["x"])})
    return _norm(_ref(db, "arr").get())


def scenario_increment(db: Any) -> Any:
    from google.cloud.firestore import Increment

    _ref(db, "cnt").delete()
    _ref(db, "cnt").set({"n": 10})
    _ref(db, "cnt").update({"n": Increment(5)})
    _ref(db, "cnt").update({"n": Increment(-3)})
    return _norm(_ref(db, "cnt").get())


def scenario_delete_field(db: Any) -> Any:
    from google.cloud.firestore import DELETE_FIELD

    _ref(db, "df").set({"keep": 1, "gone": 2, "nested": {"sub": 3}})
    _ref(db, "df").update({"gone": DELETE_FIELD, "nested.sub": DELETE_FIELD})
    return _norm(_ref(db, "df").get())


def scenario_nested_collection(db: Any) -> Any:
    nested = db.collection("shadow_users").document("uid-1").collection("chats")
    nested.document("chat1").set({"msg": "hello", "n": 1})
    nested.document("chat2").set({"msg": "world", "n": 2})
    q = nested.where("n", ">=", 2)
    return _snaps(q.stream())


def scenario_transaction_count(db: Any) -> Any:
    from google.cloud import firestore

    @firestore.transactional
    def run(tx, doc_id: str) -> int:
        ref = db.collection("shadow_tx").document(doc_id)
        # business callsite shape: DocumentReference.get(transaction=tx)
        # (real SDK Transaction.get directly returns a generator, so go through
        # the ref which both backends resolve to a single DocumentSnapshot)
        snap = ref.get(transaction=tx)
        cur = snap.to_dict().get("count", 0) if snap.exists else 0
        tx.set(ref, {"count": cur + 1})
        return cur + 1

    db.collection("shadow_tx").document("txn1").delete()
    first = run(db.transaction(), "txn1")
    second = run(db.transaction(), "txn1")
    return {"first": first, "second": second}


def scenario_server_timestamp(db: Any) -> Any:
    from google.cloud.firestore import SERVER_TIMESTAMP

    _ref(db, "ts").set({"created": SERVER_TIMESTAMP})
    snap = _ref(db, "ts").get()
    data = snap.to_dict()
    # timestamp value differs per run; assert it looks like a time and echo shape
    v = data.get("created")
    is_time = isinstance(v, datetime)
    return {"is_time": is_time, "keys": sorted(data.keys())}


def scenario_dotted_path_query(db: Any) -> Any:
    """Registry-style dotted field paths (subject.kind, promotion.required)."""
    refs = [
        (db.collection("shadow_dotted").document("a"), {"subject": {"kind": "email", "id": "e1"}, "promotion": {"required": True}}),
        (db.collection("shadow_dotted").document("b"), {"subject": {"kind": "sms", "id": "s1"}, "promotion": {"required": False}}),
        (db.collection("shadow_dotted").document("c"), {"subject": {"kind": "email", "id": "e2"}, "promotion": {"required": True}}),
    ]
    for ref, data in refs:
        ref.set(data)
    eq = db.collection("shadow_dotted").where("subject.kind", "==", "email")
    eq_bool = db.collection("shadow_dotted").where("promotion.required", "==", True)
    # combined dotted + flat multi-field (registry compound-query shape)
    both = db.collection("shadow_dotted").where("subject.kind", "==", "email").where("promotion.required", "==", True)
    return {
        "eq": _snaps(eq.stream()),
        "eq_bool": _snaps(eq_bool.stream()),
        "both": _snaps(both.stream()),
    }


def scenario_order_by_dotted(db: Any) -> Any:
    """order_by on a nested field path (registry specs sort on nested fields)."""
    stamps = [
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2023, 6, 15, tzinfo=timezone.utc),
        datetime(2025, 3, 10, tzinfo=timezone.utc),
    ]
    for i, ts in enumerate(stamps):
        db.collection("shadow_dotted_order").document(f"o{i}").set(
            {"subject": {"created": ts}, "rank": i}
        )
    q = db.collection("shadow_dotted_order").order_by("subject.created", direction="DESCENDING")
    return _snaps(q.stream())


def scenario_offset(db: Any) -> Any:
    """limit().offset() pagination (conversations/memories list queries)."""
    for i in range(5):
        db.collection("shadow_offset").document(f"p{i}").set({"n": i, "name": f"item-{i}"})
    # offset without order: real SDK default order is by doc_id; shim sorts by doc_id too
    page = list(db.collection("shadow_offset").order_by("n").offset(2).limit(2).stream())
    return _snaps(page)


def scenario_count(db: Any) -> Any:
    """Query.count() -> [[result.value]] (conversations count)."""
    for i in range(4):
        db.collection("shadow_count").document(f"c{i}").set({"kind": "x"})
    for i in range(2):
        db.collection("shadow_count").document(f"d{i}").set({"kind": "y"})
    total = db.collection("shadow_count").count().get()
    filtered = db.collection("shadow_count").where("kind", "==", "y").count().get()
    return {"total": total[0][0].value, "kind_y": filtered[0][0].value}


def scenario_array_contains(db: Any) -> Any:
    """array_contains underscore variant + composite AND filter (apps/personas)."""
    from google.cloud.firestore_v1.base_query import BaseCompositeFilter
    from google.cloud.firestore_v1 import FieldFilter as SDKFieldFilter

    db.collection("shadow_ac").document("a").set({"capabilities": ["persona", "audio"], "approved": True, "private": False})
    db.collection("shadow_ac").document("b").set({"capabilities": ["audio"], "approved": True, "private": False})
    db.collection("shadow_ac").document("c").set({"capabilities": ["persona"], "approved": False, "private": False})
    # composite AND: approved==True AND private==False
    composite = db.collection("shadow_ac").where(
        filter=BaseCompositeFilter(
            "AND",
            [SDKFieldFilter("approved", "==", True), SDKFieldFilter("private", "==", False)],
        )
    )
    # array_contains underscore
    ac = db.collection("shadow_ac").where(filter=SDKFieldFilter("capabilities", "array_contains", "persona"))
    return {"composite": _snaps(composite.stream()), "array_contains": _snaps(ac.stream())}


def scenario_doc_get_projection(db: Any) -> Any:
    """DocumentReference.get(['field']) projection (users settings)."""
    ref = db.collection("shadow_proj").document("d1")
    ref.set({"language": "en", "other": 1, "nested": {"a": 1}})
    proj = ref.get(["language", "missing"])
    return {"exists": proj.exists, "data": _norm(proj.to_dict())}


def scenario_doc_id_range(db: Any) -> Any:
    """Range filter on the reserved __name__ (doc id) field (monthly llm_usage)."""
    coll = db.collection("shadow_idrange")
    for d in ["2024-01-01", "2024-01-15", "2024-02-01", "2024-03-01"]:
        coll.document(d).set({"n": 1})
    start = coll.document("2024-01-01")
    end = coll.document("2024-02-01")
    from google.cloud.firestore_v1 import FieldFilter as SDKFieldFilter

    q = (
        coll.where(filter=SDKFieldFilter("__name__", ">=", start))
        .where(filter=SDKFieldFilter("__name__", "<", end))
    )
    return [d.id for d in q.stream()]


def scenario_select_projection(db: Any) -> Any:
    """CollectionReference.select([...]) projection (action_item_ids)."""
    coll = db.collection("shadow_sel")
    coll.document("s1").set({"a": 1, "b": 2})
    coll.document("s2").set({"a": 3, "b": 4})
    docs = [d.to_dict() for d in coll.select(["a"]).stream()]
    docs.sort(key=lambda d: d.get("a", 0))
    return docs


SCENARIOS: Dict[str, Scenario] = {
    "set_get": scenario_set_get,
    "set_merge": scenario_set_merge,
    "update_fields": scenario_update_fields,
    "delete": scenario_delete,
    "query_eq": scenario_query_eq,
    "query_comparison": scenario_query_comparison,
    "query_order_limit": scenario_query_order_limit,
    "query_in": scenario_query_in,
    "array_union_remove": scenario_array_union_remove,
    "increment": scenario_increment,
    "delete_field": scenario_delete_field,
    "nested_collection": scenario_nested_collection,
    "transaction_count": scenario_transaction_count,
    "server_timestamp": scenario_server_timestamp,
    "dotted_path_query": scenario_dotted_path_query,
    "order_by_dotted": scenario_order_by_dotted,
    "offset": scenario_offset,
    "count": scenario_count,
    "array_contains": scenario_array_contains,
    "doc_get_projection": scenario_doc_get_projection,
    "doc_id_range": scenario_doc_id_range,
    "select_projection": scenario_select_projection,
}
