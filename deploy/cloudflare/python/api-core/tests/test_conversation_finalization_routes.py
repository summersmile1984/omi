import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from conversation_finalization_routes import (  # noqa: E402
    finalize_conversation,
    get_conversation_finalization_status,
)

SECRET = "conversation-finalization-test-secret"
UID = "finalization-user"
CONVERSATION_ID = "conversation-1"


class FakeStatement:
    def __init__(self, connection, sql, args=()):
        self.connection = connection
        self.sql = sql
        self.args = args

    def bind(self, *args):
        return FakeStatement(self.connection, self.sql, args)

    async def first(self):
        row = self.connection.execute(self.sql, self.args).fetchone()
        return dict(row) if row is not None else None

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for migration in sorted(migration_dir.glob("*.sql")):
            self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            results = []
            for statement in statements:
                cursor = self.connection.execute(statement.sql, statement.args)
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                results.append({"results": rows, "meta": {"changes": cursor.rowcount}})
            self.connection.commit()
            return results
        except Exception:
            self.connection.rollback()
            raise


class FakeQueue:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


class FakeRequest:
    def __init__(self, env, *, headers=None, body=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self._body = json.dumps(body or {}).encode()

    async def body(self):
        return self._body


def auth_headers(uid=UID, *, authority="better-auth"):
    payload = json.dumps(
        {"uid": uid, "authority": authority, "requestId": "test-finalization"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def environment():
    db = FakeDb()
    queue = FakeQueue()
    env = SimpleNamespace(APP_DB=db, JOBS=queue, INTERNAL_ASSERTION_SECRET=SECRET)
    db.connection.execute(
        "INSERT INTO cf_conversations "
        "(uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
        "structured_json, transcript_segments_json, external_data_json) "
        "VALUES (?, ?, 100, 100, 100, NULL, 'desktop', 'en', 'in_progress', 'private', '{}', ?, '{}')",
        (UID, CONVERSATION_ID, json.dumps([{"text": "hello", "start": 0, "end": 2, "speaker": "SPEAKER_00"}])),
    )
    db.connection.commit()
    return env, queue


def test_finalize_admits_durable_job_and_enqueues():
    env, queue = environment()
    response = asyncio.run(
        finalize_conversation(
            FakeRequest(env, headers=auth_headers(), body={"calendar_meeting_context": {"title": "Standup"}}),
            CONVERSATION_ID,
        )
    )

    assert response["conversation"]["status"] == "processing"
    assert response["conversation"]["external_data"]["calendar_meeting_context"] == {"title": "Standup"}
    assert len(queue.messages) == 1
    message = queue.messages[0]
    assert message["kind"] == "conversation_finalize"
    assert message["payload"]["conversationId"] == CONVERSATION_ID
    job = env.APP_DB.connection.execute(
        "SELECT status, attempts, finalization_revision FROM cf_conversation_finalization_jobs"
    ).fetchone()
    assert tuple(job) == ("queued", 0, 100)


def test_finalization_status_projects_job_state():
    env, _ = environment()
    asyncio.run(finalize_conversation(FakeRequest(env, headers=auth_headers()), CONVERSATION_ID))
    response = asyncio.run(
        get_conversation_finalization_status(FakeRequest(env, headers=auth_headers()), CONVERSATION_ID)
    )
    assert response["status"] == "queued"
    assert response["terminal"] is False
    assert response["retryable"] is True
    assert response["attempt_count"] == 0


def test_finalize_is_idempotent_for_completed_conversation():
    env, queue = environment()
    env.APP_DB.connection.execute(
        "UPDATE cf_conversations SET status = 'completed', finished_at = 120 WHERE uid = ? AND id = ?",
        (UID, CONVERSATION_ID),
    )
    env.APP_DB.connection.commit()
    response = asyncio.run(finalize_conversation(FakeRequest(env, headers=auth_headers()), CONVERSATION_ID))
    assert response["conversation"]["status"] == "completed"
    assert queue.messages == []
