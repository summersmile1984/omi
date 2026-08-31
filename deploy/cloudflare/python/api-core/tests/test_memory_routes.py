import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from memory_routes import (  # noqa: E402
    create_memory,
    create_memories_batch,
    delete_memory,
    delete_memories_batch,
    list_memories,
    review_memory,
    search_product_memory,
    search_vector_memory,
    update_memory_baseline,
    update_memory_content,
    update_memory_read_status,
    update_memory_visibility,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for name in ("0032_conversations.sql", "0037_memories.sql", "0046_account_usage.sql"):
            self.connection.executescript((migration_dir / name).read_text())
        self.batch_statement_counts = []

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        self.batch_statement_counts.append(len(statements))
        try:
            for statement in statements:
                self.connection.execute(statement.sql, statement.args)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


class FakeStatement:
    def __init__(self, connection, sql):
        self.connection = connection
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def first(self):
        row = self.connection.execute(self.sql, self.args).fetchone()
        return dict(row) if row is not None else None

    async def all(self):
        rows = self.connection.execute(self.sql, self.args).fetchall()
        return {"results": [dict(row) for row in rows]}

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class FakeRequest:
    def __init__(self, env, headers, query=None, body=None):
        self.scope = {"env": env}
        self.headers = headers
        self.query_params = query or {}
        self._body = body

    async def body(self):
        return json.dumps(self._body).encode()


def signed_headers(secret: str, uid: str = "memory-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "memory-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(secret: str):
    return type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()


class FakeVectorIndex:
    def __init__(self, matches=None):
        self.matches = matches or []
        self.calls = []

    async def query(self, vector, options):
        self.calls.append((vector, options))
        return {"matches": self.matches}


class FakeVectorAi:
    async def run(self, model, payload):
        assert model == "test-vector-model"
        assert payload["text"] == ["coffee"]
        return {"data": [[0.1] * 1024]}


def create(env, secret: str, *, uid: str = "memory-user", **body):
    payload = {
        "content": body.pop("content", "Remember this"),
        "category": body.pop("category", "manual"),
        "visibility": body.pop("visibility", "private"),
        **body,
    }
    return asyncio.run(create_memory(FakeRequest(env, signed_headers(secret, uid), body=payload)))


def test_memory_list_is_authenticated_bounded_and_empty_for_a_new_account():
    secret = "memory-secret"
    env = make_env(secret)

    assert asyncio.run(list_memories(FakeRequest(env, signed_headers(secret)))) == []
    assert asyncio.run(list_memories(FakeRequest(env, {}))).status_code == 401
    invalid = asyncio.run(list_memories(FakeRequest(env, signed_headers(secret), {"limit": "501"})))
    assert invalid.status_code == 400
    blank = create(env, secret, content="   ")
    assert blank.status_code == 400


def test_product_memory_search_uses_d1_default_visibility_and_contract():
    secret = "memory-secret"
    env = make_env(secret)
    manual = create(env, secret, content="Coffee before software review", category="manual")
    automatic = create(env, secret, content="Coffee notes from today", category="interesting")
    archive = create(env, secret, content="Coffee archive record", category="manual")
    reviewed = create(env, secret, content="Coffee rejected record", category="manual")
    locked = create(env, secret, content="Coffee locked record", category="manual")
    create(env, secret, uid="other-user", content="Coffee belongs to another user")
    env.APP_DB.connection.execute(
        "UPDATE cf_memories SET memory_tier = 'archive' WHERE uid = ? AND id = ?",
        ("memory-user", archive["id"]),
    )
    env.APP_DB.connection.execute(
        "UPDATE cf_memories SET user_review = 0 WHERE uid = ? AND id = ?",
        ("memory-user", reviewed["id"]),
    )
    env.APP_DB.connection.execute(
        "UPDATE cf_memories SET is_locked = 1 WHERE uid = ? AND id = ?",
        ("memory-user", locked["id"]),
    )
    env.APP_DB.connection.commit()

    page = asyncio.run(
        search_product_memory(
            FakeRequest(env, signed_headers(secret), {"query": "coffee", "limit": "1", "offset": "0"})
        )
    )
    assert page["uid"] == "memory-user"
    assert page["query"] == "coffee"
    assert page["total_count"] == 2
    assert page["returned_count"] == 1
    assert page["limit"] == 1
    assert page["offset"] == 0
    assert page["archive_default_visible"] is False
    assert page["policy"] == {
        "consumer": "omi_chat",
        "app_has_default_memory_grant": True,
        "archive_capability": False,
        "raw_provenance_capability": False,
    }
    assert page["global_read_gate"]["read_decision"] == "USE_MEMORY"
    assert page["rollout"]["surface"] == "product_default_search"
    assert page["rollout"]["capabilities"]["legacy_reads_authoritative"] is False
    item = page["items"][0]
    assert item["memory_id"] in {manual["id"], automatic["id"]}
    assert item["memory_layer"] == "product_memory"
    assert item["tier"] in {"long_term", "short_term"}
    assert item["lifecycle_status"] == "active"
    assert item["processing_state"] == "processed"
    assert item["agent_use"] == "default_access_memory"
    assert item["access_reason"] == "default_memory_allowed"
    assert isinstance(item["date"], str)

    second_page = asyncio.run(
        search_product_memory(
            FakeRequest(env, signed_headers(secret), {"query": "coffee", "limit": "1", "offset": "1"})
        )
    )
    assert second_page["returned_count"] == 1
    assert {page["items"][0]["memory_id"], second_page["items"][0]["memory_id"]} == {
        manual["id"],
        automatic["id"],
    }

    assert asyncio.run(search_product_memory(FakeRequest(env, {}))).status_code == 401
    assert (
        asyncio.run(search_product_memory(FakeRequest(env, signed_headers(secret), {"limit": "0"}))).status_code == 400
    )
    assert (
        asyncio.run(search_product_memory(FakeRequest(env, signed_headers(secret), {"query": "x" * 501}))).status_code
        == 400
    )

    env.APP_DB.connection.execute("DROP TABLE cf_memories")
    env.APP_DB.connection.commit()
    assert (
        asyncio.run(search_product_memory(FakeRequest(env, signed_headers(secret), {"query": "coffee"}))).status_code
        == 503
    )


def test_vector_memory_search_uses_vectorize_candidates_and_d1_hydration():
    secret = "memory-secret"
    env = make_env(secret)
    env.AI = FakeVectorAi()
    vector_id = "a" * 64
    env.MEMORY_VECTORS = FakeVectorIndex([{"id": vector_id, "score": 0.91}])
    env.WORKERS_AI_VECTOR_MODEL = "test-vector-model"
    env.APP_DB.connection.execute(
        "CREATE TABLE cf_vector_projection_state ("
        "uid TEXT NOT NULL, projection_kind TEXT NOT NULL, source_id TEXT NOT NULL, sub_id TEXT NOT NULL, "
        "vector_id TEXT NOT NULL, source_version INTEGER NOT NULL, model TEXT NOT NULL, updated_at INTEGER NOT NULL)"
    )
    env.APP_DB.connection.commit()

    memory = create(env, secret, content="Coffee before vector search", category="manual")
    env.APP_DB.connection.execute(
        "INSERT INTO cf_vector_projection_state "
        "(uid, projection_kind, source_id, sub_id, vector_id, source_version, model, updated_at) "
        "VALUES (?, 'memory', ?, '', ?, 7, 'test-vector-model', 1)",
        ("memory-user", memory["id"], vector_id),
    )
    env.APP_DB.connection.commit()

    response = asyncio.run(
        search_vector_memory(FakeRequest(env, signed_headers(secret), {"query": "coffee", "limit": "1"}))
    )
    assert response["uid"] == "memory-user"
    assert response["query"] == "coffee"
    assert response["returned_count"] == 1
    assert response["items"][0]["id"] == memory["id"]
    assert response["scores_by_memory_id"] == {memory["id"]: 0.91}
    assert response["projection_commit_ids_by_memory_id"] == {memory["id"]: "7"}
    assert response["legacy_fallback_used"] is False
    assert response["rollout"]["surface"] == "product_vector_search"
    assert env.MEMORY_VECTORS.calls[0][1]["namespace"]

    env.APP_DB.connection.execute(
        "UPDATE cf_memories SET is_locked = 1 WHERE uid = ? AND id = ?", ("memory-user", memory["id"])
    )
    env.APP_DB.connection.commit()
    locked = asyncio.run(
        search_vector_memory(FakeRequest(env, signed_headers(secret), {"query": "coffee", "limit": "1"}))
    )
    assert locked["items"] == []
    assert locked["hydration_rejected_access_denied_count"] == 1

    assert asyncio.run(search_vector_memory(FakeRequest(env, {}))).status_code == 401
    assert asyncio.run(search_vector_memory(FakeRequest(env, signed_headers(secret), {"query": ""}))).status_code == 400


def test_memory_create_list_filters_and_preserves_canonical_shape_with_uid_isolation():
    secret = "memory-secret"
    env = make_env(secret)

    manual = create(
        env,
        secret,
        content="Lives in Shanghai",
        category="manual",
        visibility="public",
        tags=["profile"],
        predicate="resides_in",
        arguments={"place": "Shanghai"},
        subject_attribution="user",
    )
    automatic = create(env, secret, content="Likes tea", category="interesting")
    create(env, secret, uid="other-user", content="Other user's memory")

    assert manual["memory_id"] == manual["id"]
    assert manual["uid"] == "memory-user"
    assert manual["content"] == "Lives in Shanghai"
    assert manual["memory_tier"] == "long_term"
    assert manual["layer"] == "long_term"
    assert manual["manually_added"] is True
    assert manual["arguments"] == {"place": "Shanghai"}
    assert manual["created_at"].endswith("+00:00")
    assert automatic["memory_tier"] == "short_term"
    usage = env.APP_DB.connection.execute(
        "SELECT memories_created FROM cf_usage_sources WHERE uid = ? AND source_kind = 'memory'",
        ("memory-user",),
    ).fetchall()
    assert [row["memories_created"] for row in usage] == [1, 1]

    listed = asyncio.run(list_memories(FakeRequest(env, signed_headers(secret), {"limit": "10", "offset": "0"})))
    assert {item["id"] for item in listed} == {manual["id"], automatic["id"]}
    filtered = asyncio.run(list_memories(FakeRequest(env, signed_headers(secret), {"categories": "manual"})))
    assert [item["id"] for item in filtered] == [manual["id"]]


def test_memory_batch_create_is_atomic_bounded_and_drops_per_file_imports():
    secret = "memory-secret"
    env = make_env(secret)
    response = asyncio.run(
        create_memories_batch(
            FakeRequest(
                env,
                signed_headers(secret),
                body={
                    "memories": [
                        {"content": "Manual batch memory", "category": "manual", "tags": ["profile"]},
                        {"content": "Automatic batch memory", "category": "interesting"},
                        {
                            "content": "Per-file import must be dropped",
                            "category": "system",
                            "tags": ["local-files", "onboarding", "recent file"],
                        },
                    ]
                },
            )
        )
    )
    assert response["created_count"] == 2
    assert [memory["content"] for memory in response["memories"]] == [
        "Manual batch memory",
        "Automatic batch memory",
    ]
    assert response["memories"][0]["memory_tier"] == "long_term"
    assert response["memories"][1]["memory_tier"] == "short_term"
    assert env.APP_DB.batch_statement_counts == [2]
    assert env.APP_DB.connection.execute("SELECT COUNT(*) FROM cf_memories").fetchone()[0] == 2
    assert env.APP_DB.connection.execute("SELECT COUNT(*) FROM cf_usage_sources").fetchone()[0] == 2

    empty = asyncio.run(create_memories_batch(FakeRequest(env, signed_headers(secret), body={"memories": []})))
    assert empty == {"memories": [], "created_count": 0}
    assert env.APP_DB.batch_statement_counts == [2]

    full_env = make_env(secret)
    full = asyncio.run(
        create_memories_batch(
            FakeRequest(
                full_env,
                signed_headers(secret),
                body={"memories": [{"content": f"Memory {index}"} for index in range(100)]},
            )
        )
    )
    assert full["created_count"] == 100
    assert full_env.APP_DB.batch_statement_counts == [2]

    chunked_env = make_env(secret)
    chunked = asyncio.run(
        create_memories_batch(
            FakeRequest(
                chunked_env,
                signed_headers(secret),
                body={"memories": [{"content": "x" * 50_000} for _ in range(40)]},
            )
        )
    )
    assert chunked["created_count"] == 40
    assert chunked_env.APP_DB.batch_statement_counts == [4]

    oversized = asyncio.run(
        create_memories_batch(
            FakeRequest(
                make_env(secret),
                signed_headers(secret),
                body={"memories": [{"content": f"Memory {index}"} for index in range(101)]},
            )
        )
    )
    assert oversized.status_code == 422
    missing = asyncio.run(create_memories_batch(FakeRequest(make_env(secret), signed_headers(secret), body={})))
    assert missing.status_code == 422

    failing_env = make_env(secret)
    failing_env.APP_DB.connection.execute("DROP TABLE cf_usage_sources")
    failing_env.APP_DB.connection.commit()
    failed = asyncio.run(
        create_memories_batch(
            FakeRequest(
                failing_env,
                signed_headers(secret),
                body={"memories": [{"content": "Must roll back"}]},
            )
        )
    )
    assert failed.status_code == 503
    assert failing_env.APP_DB.connection.execute("SELECT COUNT(*) FROM cf_memories").fetchone()[0] == 0


def test_memory_edit_visibility_review_and_delete_are_uid_scoped():
    secret = "memory-secret"
    env = make_env(secret)
    memory = create(env, secret)
    memory_id = memory["id"]

    assert asyncio.run(
        update_memory_content(FakeRequest(env, signed_headers(secret), {"value": "Edited memory"}), memory_id)
    ) == {"status": "ok"}
    assert asyncio.run(
        update_memory_visibility(FakeRequest(env, signed_headers(secret), {"value": "public"}), memory_id)
    ) == {"status": "ok"}
    assert asyncio.run(review_memory(FakeRequest(env, signed_headers(secret), {"value": "false"}), memory_id)) == {
        "status": "ok"
    }

    listed = asyncio.run(list_memories(FakeRequest(env, signed_headers(secret))))
    assert listed[0]["content"] == "Edited memory"
    assert listed[0]["edited"] is True
    assert listed[0]["visibility"] == "public"
    assert listed[0]["reviewed"] is True
    assert listed[0]["user_review"] is False

    other_delete = asyncio.run(delete_memory(FakeRequest(env, signed_headers(secret, "other-user")), memory_id))
    assert other_delete.status_code == 404
    assert asyncio.run(delete_memory(FakeRequest(env, signed_headers(secret)), memory_id)) == {"status": "ok"}
    assert asyncio.run(list_memories(FakeRequest(env, signed_headers(secret)))) == []

    tombstone = env.APP_DB.connection.execute(
        "SELECT deleted_at FROM cf_memories WHERE uid = ? AND id = ?",
        ("memory-user", memory_id),
    ).fetchone()
    assert tombstone["deleted_at"] is not None


def test_memory_read_and_baseline_status_are_uid_scoped_and_locked():
    secret = "memory-secret"
    env = make_env(secret)
    memory = create(env, secret)
    memory_id = memory["id"]

    read = asyncio.run(
        update_memory_read_status(
            FakeRequest(
                env,
                signed_headers(secret),
                body={"is_read": True, "is_dismissed": True},
            ),
            memory_id,
        )
    )
    assert read["id"] == memory_id
    assert read["is_read"] is True
    assert read["is_dismissed"] is True
    partial = asyncio.run(
        update_memory_read_status(
            FakeRequest(env, signed_headers(secret), body={"is_read": False}),
            memory_id,
        )
    )
    assert partial["is_read"] is False
    assert partial["is_dismissed"] is True
    assert asyncio.run(
        update_memory_baseline(
            FakeRequest(env, signed_headers(secret), {"value": "yes"}),
            memory_id,
        )
    ) == {"status": "ok"}
    persisted = env.APP_DB.connection.execute(
        "SELECT is_read, is_dismissed, is_baseline FROM cf_memories WHERE uid = ? AND id = ?",
        ("memory-user", memory_id),
    ).fetchone()
    assert dict(persisted) == {"is_read": 0, "is_dismissed": 1, "is_baseline": 1}

    missing_value = asyncio.run(
        update_memory_read_status(
            FakeRequest(env, signed_headers(secret), body={}),
            memory_id,
        )
    )
    assert missing_value.status_code == 422
    unexpected_value = asyncio.run(
        update_memory_read_status(
            FakeRequest(env, signed_headers(secret), body={"is_read": True, "unexpected": True}),
            memory_id,
        )
    )
    assert unexpected_value.status_code == 422
    other_user = asyncio.run(
        update_memory_baseline(
            FakeRequest(env, signed_headers(secret, "other-user"), {"value": "false"}),
            memory_id,
        )
    )
    assert other_user.status_code == 404

    env.APP_DB.connection.execute(
        "UPDATE cf_memories SET is_locked = 1 WHERE uid = ? AND id = ?",
        ("memory-user", memory_id),
    )
    env.APP_DB.connection.commit()
    locked = asyncio.run(
        update_memory_read_status(
            FakeRequest(env, signed_headers(secret), body={"is_read": False}),
            memory_id,
        )
    )
    assert locked.status_code == 402


def test_batch_delete_is_all_or_nothing_and_keeps_tombstones():
    secret = "memory-secret"
    env = make_env(secret)
    first = create(env, secret, content="First")
    second = create(env, secret, content="Second")

    missing = asyncio.run(
        delete_memories_batch(
            FakeRequest(
                env,
                signed_headers(secret),
                body={"memory_ids": [first["id"], "missing"]},
            )
        )
    )
    assert missing.status_code == 404
    assert len(asyncio.run(list_memories(FakeRequest(env, signed_headers(secret))))) == 2

    deleted = asyncio.run(
        delete_memories_batch(
            FakeRequest(
                env,
                signed_headers(secret),
                body={"memory_ids": [first["id"], second["id"]]},
            )
        )
    )
    assert deleted == {"status": "ok"}
    assert asyncio.run(list_memories(FakeRequest(env, signed_headers(secret)))) == []
    count = env.APP_DB.connection.execute(
        "SELECT COUNT(*) AS count FROM cf_memories WHERE uid = ? AND deleted_at IS NOT NULL",
        ("memory-user",),
    ).fetchone()
    assert count["count"] == 2
