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
    delete_memory,
    delete_memories_batch,
    list_memories,
    review_memory,
    update_memory_content,
    update_memory_visibility,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for name in ("0032_conversations.sql", "0037_memories.sql", "0046_account_usage.sql"):
            self.connection.executescript((migration_dir / name).read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
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
