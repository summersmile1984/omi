import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from chat_routes import (  # noqa: E402
    clear_messages,
    get_messages,
    get_shared_chat_messages,
    share_chat_messages,
)


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
        self.connection.executescript((migration_dir / "0042_chat_messages.sql").read_text())
        self.connection.executescript((migration_dir / "0044_chat_shares.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        results = []
        try:
            self.connection.execute("BEGIN")
            for statement in statements:
                cursor = self.connection.execute(statement.sql, statement.args)
                results.append({"meta": {"changes": cursor.rowcount}})
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return results


class FakeRequest:
    def __init__(self, env, headers=None, query=None, body=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.query_params = query or {}
        self.body = body

    async def json(self):
        return self.body


def signed_headers(secret: str, uid: str = "chat-user", display_name: str | None = None):
    context = {"uid": uid}
    if display_name is not None:
        context["displayName"] = display_name
    raw = json.dumps(context, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def test_empty_chat_history_returns_worker_owned_initial_message():
    secret = "chat-secret"
    db = FakeDb()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    result = asyncio.run(get_messages(FakeRequest(env, signed_headers(secret), {"app_id": "assistant"})))
    assert len(result) == 1
    assert result[0]["sender"] == "ai"
    assert result[0]["app_id"] == "assistant"


def test_chat_history_is_scoped_by_uid_and_app_and_clear_returns_initial_message():
    secret = "chat-secret"
    db = FakeDb()
    messages = [
        (
            "chat-user",
            "m1",
            "assistant",
            20,
            {"id": "m1", "text": "hello", "sender": "human", "type": "text", "created_at": "2026-08-28T00:00:00Z"},
        ),
        (
            "chat-user",
            "m2",
            None,
            30,
            {"id": "m2", "text": "default", "sender": "human", "type": "text", "created_at": "2026-08-28T00:01:00Z"},
        ),
        (
            "other-user",
            "m3",
            "assistant",
            40,
            {"id": "m3", "text": "other", "sender": "human", "type": "text", "created_at": "2026-08-28T00:02:00Z"},
        ),
    ]
    db.connection.executemany(
        "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) VALUES (?, ?, ?, ?, ?)",
        [(uid, mid, app, created, json.dumps(message)) for uid, mid, app, created, message in messages],
    )
    db.connection.commit()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    scoped = asyncio.run(get_messages(FakeRequest(env, signed_headers(secret), {"app_id": "assistant"})))
    assert [message["id"] for message in scoped] == ["m1"]
    cleared = asyncio.run(clear_messages(FakeRequest(env, signed_headers(secret), {"app_id": "assistant"})))
    assert cleared["app_id"] == "assistant"
    assert (
        db.connection.execute(
            "SELECT COUNT(*) FROM cf_chat_messages WHERE uid = 'chat-user' AND app_id = 'assistant'"
        ).fetchone()[0]
        == 0
    )
    default = asyncio.run(get_messages(FakeRequest(env, signed_headers(secret))))
    assert [message["id"] for message in default] == ["m2"]


def test_chat_history_requires_better_auth_context():
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": "chat-secret"})()
    result = asyncio.run(get_messages(FakeRequest(env, {})))
    assert result.status_code == 401


def test_chat_share_is_ordered_public_bounded_and_d1_owned():
    secret = "chat-secret"
    db = FakeDb()
    messages = [
        (
            "chat-user",
            "m1",
            None,
            10,
            {"id": "m1", "text": "First", "sender": "human", "created_at": "2026-08-28T00:00:00Z"},
        ),
        (
            "chat-user",
            "m2",
            None,
            20,
            {
                "id": "m2",
                "text": "Second",
                "sender": "ai",
                "created_at": "2026-08-28T00:01:00Z",
                "files": [{"secret": "must-not-leak"}],
            },
        ),
    ]
    db.connection.executemany(
        "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) VALUES (?, ?, ?, ?, ?)",
        [(uid, mid, app, created, json.dumps(message)) for uid, mid, app, created, message in messages],
    )
    db.connection.commit()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    shared = asyncio.run(
        share_chat_messages(
            FakeRequest(
                env,
                signed_headers(secret, display_name="Alice"),
                body={"message_ids": ["m2", "m1"]},
            )
        )
    )
    assert shared["url"] == f"https://h.omi.me/chat/{shared['token']}"

    preview = asyncio.run(get_shared_chat_messages(FakeRequest(env), shared["token"]))
    assert preview == {
        "sender_name": "Alice",
        "messages": [
            {"id": "m2", "text": "Second", "sender": "ai", "created_at": "2026-08-28T00:01:00Z"},
            {"id": "m1", "text": "First", "sender": "human", "created_at": "2026-08-28T00:00:00Z"},
        ],
        "count": 2,
    }


def test_chat_share_rejects_missing_duplicate_unauthorized_and_expired_paths():
    secret = "chat-secret"
    db = FakeDb()
    db.connection.execute(
        "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) VALUES (?, ?, NULL, ?, ?)",
        ("chat-user", "m1", 10, json.dumps({"id": "m1", "text": "First", "sender": "human"})),
    )
    db.connection.commit()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    unauthorized = asyncio.run(share_chat_messages(FakeRequest(env, body={"message_ids": ["m1"]})))
    assert unauthorized.status_code == 401
    missing = asyncio.run(
        share_chat_messages(FakeRequest(env, signed_headers(secret), body={"message_ids": ["missing"]}))
    )
    assert missing.status_code == 404
    duplicate = asyncio.run(
        share_chat_messages(FakeRequest(env, signed_headers(secret), body={"message_ids": ["m1", "m1"]}))
    )
    assert duplicate.status_code == 400

    shared = asyncio.run(share_chat_messages(FakeRequest(env, signed_headers(secret), body={"message_ids": ["m1"]})))
    db.connection.execute("UPDATE cf_chat_shares SET expires_at = 0 WHERE token = ?", (shared["token"],))
    db.connection.commit()
    expired = asyncio.run(get_shared_chat_messages(FakeRequest(env), shared["token"]))
    assert expired.status_code == 404

    asyncio.run(share_chat_messages(FakeRequest(env, signed_headers(secret), body={"message_ids": ["m1"]})))
    assert (
        db.connection.execute("SELECT COUNT(*) FROM cf_chat_shares WHERE token = ?", (shared["token"],)).fetchone()[0]
        == 0
    )
    assert (
        db.connection.execute(
            "SELECT COUNT(*) FROM cf_chat_share_messages WHERE token = ?", (shared["token"],)
        ).fetchone()[0]
        == 0
    )
