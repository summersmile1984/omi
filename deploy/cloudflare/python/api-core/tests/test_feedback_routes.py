import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from feedback_routes import (  # noqa: E402
    get_memory_summary_rating,
    set_chat_message_rating,
    set_memory_summary_rating,
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

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        return {"meta": {"changes": cursor.rowcount}}


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.fail_next_batch = False
        self.connection.executescript(
            "CREATE TABLE cf_account_deletion_intents (uid TEXT PRIMARY KEY);"
            "CREATE TABLE cf_account_deletion_tombstones (uid TEXT PRIMARY KEY);"
        )
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0042_chat_messages.sql").read_text())
        self.connection.executescript((migration_dir / "0053_user_feedback.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        if self.fail_next_batch:
            self.fail_next_batch = False
            raise RuntimeError("simulated batch failure")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            results = [await statement.run() for statement in statements]
            self.connection.commit()
            return results
        except Exception:
            self.connection.rollback()
            raise


class FakeRequest:
    def __init__(self, env, headers, query=None):
        self.scope = {"env": env}
        self.headers = headers
        self.query_params = query or {}


def signed_headers(secret: str, uid: str = "feedback-user"):
    raw = json.dumps({"uid": uid}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def environment(db, secret="feedback-secret"):
    return type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()


def response_json(response):
    return json.loads(response.body)


def test_memory_summary_feedback_is_uid_scoped_and_preserves_legacy_rating_shape():
    secret = "feedback-secret"
    db = FakeDb()
    env = environment(db, secret)
    stored = asyncio.run(
        set_memory_summary_rating(FakeRequest(env, signed_headers(secret), {"memory_id": "memory-1", "value": "1"}))
    )
    assert stored == {"status": "ok"}

    rating = asyncio.run(get_memory_summary_rating(FakeRequest(env, signed_headers(secret), {"memory_id": "memory-1"})))
    assert rating == {"has_rating": True, "rating": 1}
    other_user = asyncio.run(
        get_memory_summary_rating(FakeRequest(env, signed_headers(secret, "other-user"), {"memory_id": "memory-1"}))
    )
    assert other_user == {"has_rating": False, "rating": None}

    asyncio.run(
        set_memory_summary_rating(FakeRequest(env, signed_headers(secret), {"memory_id": "memory-1", "value": "-1"}))
    )
    hidden = asyncio.run(get_memory_summary_rating(FakeRequest(env, signed_headers(secret), {"memory_id": "memory-1"})))
    assert hidden == {"has_rating": False, "rating": -1}


def test_chat_feedback_updates_analytics_and_message_projection_atomically():
    secret = "feedback-secret"
    db = FakeDb()
    env = environment(db, secret)
    db.connection.execute(
        "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) VALUES (?, ?, NULL, ?, ?)",
        ("feedback-user", "message-1", 1, json.dumps({"id": "message-1", "text": "answer"})),
    )
    db.connection.commit()

    stored = asyncio.run(
        set_chat_message_rating(
            FakeRequest(
                env,
                signed_headers(secret),
                {"message_id": "message-1", "value": "-1", "reason": "not_helpful_or_irrelevant"},
            )
        )
    )
    assert stored == {"status": "ok"}
    feedback = db.connection.execute(
        "SELECT uid, feedback_type, subject_id, value, reason FROM cf_user_feedback"
    ).fetchone()
    assert dict(feedback) == {
        "uid": "feedback-user",
        "feedback_type": "chat_message",
        "subject_id": "message-1",
        "value": -1,
        "reason": "not_helpful_or_irrelevant",
    }
    message = db.connection.execute(
        "SELECT message_json FROM cf_chat_messages WHERE uid = ? AND id = ?",
        ("feedback-user", "message-1"),
    ).fetchone()
    assert json.loads(message["message_json"])["rating"] == -1

    asyncio.run(
        set_chat_message_rating(FakeRequest(env, signed_headers(secret), {"message_id": "message-1", "value": "0"}))
    )
    message = db.connection.execute(
        "SELECT message_json FROM cf_chat_messages WHERE uid = ? AND id = ?",
        ("feedback-user", "message-1"),
    ).fetchone()
    assert json.loads(message["message_json"])["rating"] is None


def test_feedback_rejects_invalid_input_and_fails_closed_on_d1_errors():
    secret = "feedback-secret"
    db = FakeDb()
    env = environment(db, secret)
    unauthorized = asyncio.run(set_memory_summary_rating(FakeRequest(env, {}, {"memory_id": "memory-1", "value": "1"})))
    assert unauthorized.status_code == 401
    invalid = asyncio.run(
        set_chat_message_rating(FakeRequest(env, signed_headers(secret), {"message_id": "message-1", "value": "2"}))
    )
    assert invalid.status_code == 400

    db.fail_next_batch = True
    failed = asyncio.run(
        set_chat_message_rating(FakeRequest(env, signed_headers(secret), {"message_id": "message-1", "value": "1"}))
    )
    assert failed.status_code == 503
    assert response_json(failed) == {"error": "feedback unavailable"}
    assert db.connection.execute("SELECT COUNT(*) FROM cf_user_feedback").fetchone()[0] == 0
