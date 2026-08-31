import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from memory_review_routes import (  # noqa: E402
    get_memory_review_item,
    list_memory_review_queue,
    resolve_memory_review_item,
)
from memory_routes import create_memory  # noqa: E402


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


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0037_memories.sql").read_text())
        self.connection.executescript("""
            CREATE TABLE cf_account_deletion_intents (uid TEXT PRIMARY KEY);
            CREATE TABLE cf_account_deletion_tombstones (uid TEXT PRIMARY KEY);
            """)
        self.connection.executescript((migration_dir / "0093_memory_review_queue.sql").read_text())
        self.connection.executescript("""
            CREATE TABLE cf_usage_sources (
              uid TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
              occurred_at INTEGER NOT NULL, transcription_seconds INTEGER NOT NULL,
              words_transcribed INTEGER NOT NULL, insights_gained INTEGER NOT NULL,
              memories_created INTEGER NOT NULL, updated_at INTEGER NOT NULL,
              PRIMARY KEY(uid, source_kind, source_id)
            );
            """)

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        self.connection.execute("BEGIN")
        try:
            for statement in statements:
                self.connection.execute(statement.sql, statement.args)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


class FakeRequest:
    def __init__(self, env, headers, query=None, body=None):
        self.scope = {"env": env}
        self.headers = headers
        self.query_params = query or {}
        self._body = body

    async def body(self):
        return json.dumps(self._body if self._body is not None else {}).encode()


def signed_headers(secret: str, uid: str = "review-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "review-test"},
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


def create(env, secret: str, *, content: str, arguments: dict[str, object], veracity: float):
    return asyncio.run(
        create_memory(
            FakeRequest(
                env,
                signed_headers(secret),
                body={
                    "content": content,
                    "category": "system",
                    "predicate": "resides_in",
                    "arguments": arguments,
                    "subject_entity_id": "user",
                    "veracity": veracity,
                },
            )
        )
    )


def test_review_queue_is_produced_from_structural_d1_conflict_and_is_uid_scoped():
    secret = "review-secret"
    env = make_env(secret)
    create(env, secret, content="Lives in NYC", arguments={"location": "NYC"}, veracity=0.9)
    candidate = create(env, secret, content="Lives in SF", arguments={"location": "SF"}, veracity=0.4)

    page = asyncio.run(list_memory_review_queue(FakeRequest(env, signed_headers(secret))))
    assert len(page) == 1
    item = page[0]
    assert item["fact_id"] == candidate["id"]
    assert item["conflict_with"]
    assert item["candidate"]["content"] == "Lives in SF"
    assert item["authority"] == "canonical_memory"
    assert item["permitted_uses"] == ["answers_with_disclaimer"]
    assert asyncio.run(list_memory_review_queue(FakeRequest(env, {}))).status_code == 401
    assert (
        asyncio.run(list_memory_review_queue(FakeRequest(env, signed_headers(secret), {"limit": "501"}))).status_code
        == 400
    )


def test_review_queue_source_projection_tombstones_changed_candidate():
    secret = "review-secret"
    env = make_env(secret)
    create(env, secret, content="Lives in NYC", arguments={"location": "NYC"}, veracity=0.9)
    candidate = create(env, secret, content="Lives in SF", arguments={"location": "SF"}, veracity=0.4)
    item = asyncio.run(list_memory_review_queue(FakeRequest(env, signed_headers(secret))))[0]
    env.APP_DB.connection.execute(
        "UPDATE cf_memories SET content = 'Lives in LA', updated_at = updated_at + 1 WHERE uid = ? AND id = ?",
        ("review-user", candidate["id"]),
    )
    env.APP_DB.connection.commit()

    assert asyncio.run(list_memory_review_queue(FakeRequest(env, signed_headers(secret)))) == []
    projected = asyncio.run(get_memory_review_item(FakeRequest(env, signed_headers(secret)), item["review_id"]))
    assert projected["status"] == "tombstoned"
    assert projected["candidate"] == {"id": candidate["id"]}
    assert projected["permitted_uses"] == []


def test_review_queue_accept_resolves_candidate_and_invalidates_conflict():
    secret = "review-secret"
    env = make_env(secret)
    old = create(env, secret, content="Lives in NYC", arguments={"location": "NYC"}, veracity=0.9)
    create(env, secret, content="Lives in SF", arguments={"location": "SF"}, veracity=0.4)
    item = asyncio.run(list_memory_review_queue(FakeRequest(env, signed_headers(secret))))[0]

    invalid = asyncio.run(
        resolve_memory_review_item(
            FakeRequest(env, signed_headers(secret), body={"decision": "unknown"}), item["review_id"]
        )
    )
    assert invalid.status_code == 400

    resolved = asyncio.run(
        resolve_memory_review_item(
            FakeRequest(
                env,
                signed_headers(secret),
                body={"decision": "accept", "reason": "new evidence"},
            ),
            item["review_id"],
        )
    )
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == "accept"
    assert resolved["commit"]["commit_id"].startswith("d1-review:")
    assert resolved["item"]["status"] == "accepted"
    old_row = env.APP_DB.connection.execute(
        "SELECT invalid_at, superseded_by FROM cf_memories WHERE uid = ? AND id = ?",
        ("review-user", old["id"]),
    ).fetchone()
    assert old_row[0] is not None
    assert old_row[1] == item["fact_id"]
    retry = asyncio.run(
        resolve_memory_review_item(
            FakeRequest(env, signed_headers(secret), body={"decision": "accept"}), item["review_id"]
        )
    )
    assert retry["status"] == "already_resolved"
