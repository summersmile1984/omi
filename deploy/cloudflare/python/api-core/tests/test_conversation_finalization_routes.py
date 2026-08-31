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
    process_in_progress_conversation,
    process_conversation_finalization,
    reprocess_conversation,
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

    async def all(self):
        return {"results": [dict(row) for row in self.connection.execute(self.sql, self.args).fetchall()]}

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
    def __init__(self, env, *, headers=None, body=None, query_params=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.query_params = query_params or {}
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


def test_root_conversation_finalize_uses_newest_d1_in_progress_row():
    env, queue = environment()
    response = asyncio.run(
        process_in_progress_conversation(
            FakeRequest(env, headers=auth_headers(), body={"calendar_meeting_context": {"title": "Standup"}})
        )
    )

    assert response["conversation"]["id"] == CONVERSATION_ID
    assert response["conversation"]["status"] == "processing"
    assert len(queue.messages) == 1
    assert queue.messages[0]["kind"] == "conversation_finalize"


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


def test_reprocess_admits_parameterized_job_and_preserves_conversation_wire_shape():
    env, queue = environment()
    env.APP_DB.connection.execute(
        "UPDATE cf_conversations SET status = 'completed', finished_at = 120, discarded = 1 WHERE uid = ? AND id = ?",
        (UID, CONVERSATION_ID),
    )
    env.APP_DB.connection.commit()
    response = asyncio.run(
        reprocess_conversation(
            FakeRequest(
                env,
                headers=auth_headers(),
                query_params={"language_code": "fr", "app_id": "calendar-app"},
            ),
            CONVERSATION_ID,
        )
    )

    assert response["id"] == CONVERSATION_ID
    assert response["status"] == "processing"
    assert len(queue.messages) == 1
    assert queue.messages[0] == {
        "jobId": queue.messages[0]["jobId"],
        "uid": UID,
        "kind": "conversation_reprocess",
        "payload": {
            "conversationId": CONVERSATION_ID,
            "revision": queue.messages[0]["payload"]["revision"],
            "languageCode": "fr",
            "appId": "calendar-app",
        },
    }
    job = env.APP_DB.connection.execute(
        "SELECT operation, language_code, app_id, status FROM cf_conversation_finalization_jobs"
    ).fetchone()
    assert tuple(job) == ("reprocess", "fr", "calendar-app", "queued")


def test_reprocess_is_idempotent_while_job_is_processing():
    env, queue = environment()
    first = asyncio.run(reprocess_conversation(FakeRequest(env, headers=auth_headers()), CONVERSATION_ID))
    second = asyncio.run(reprocess_conversation(FakeRequest(env, headers=auth_headers()), CONVERSATION_ID))

    assert first["id"] == second["id"] == CONVERSATION_ID
    assert first["status"] == second["status"] == "processing"
    assert len(queue.messages) == 1


def test_reprocess_returns_not_found_for_missing_d1_conversation():
    env, queue = environment()
    env.APP_DB.connection.execute("DELETE FROM cf_conversations WHERE uid = ? AND id = ?", (UID, CONVERSATION_ID))
    env.APP_DB.connection.commit()
    response = asyncio.run(reprocess_conversation(FakeRequest(env, headers=auth_headers()), CONVERSATION_ID))

    assert response.status_code == 404
    assert queue.messages == []


def test_reprocess_processor_replaces_derived_rows_and_restores_external_data(monkeypatch):
    env, queue = environment()
    env.APP_DB.connection.execute(
        "UPDATE cf_conversations SET status = 'completed', finished_at = 120, discarded = 1, "
        "external_data_json = '{\"calendar_event_id\":\"event-1\"}' WHERE uid = ? AND id = ?",
        (UID, CONVERSATION_ID),
    )
    env.APP_DB.connection.commit()
    asyncio.run(reprocess_conversation(FakeRequest(env, headers=auth_headers()), CONVERSATION_ID))
    env.APP_DB.connection.execute(
        "INSERT INTO cf_action_items (uid, id, description, status, source, provenance_json, conversation_id, created_at, updated_at) "
        "VALUES (?, 'old-action', 'old action', 'active', 'developer', '[]', ?, 100, 100)",
        (UID, CONVERSATION_ID),
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_memories (uid, id, content, memory_tier, valid_at, conversation_id, created_at, updated_at) "
        "VALUES (?, 'old-memory', 'old memory', 'short_term', 100, ?, 100, 100)",
        (UID, CONVERSATION_ID),
    )
    env.APP_DB.connection.commit()

    async def fake_targets(_env, _uid):
        return [], None

    async def fake_enrichment(_env, _transcript, _language):
        return {
            "structured": {
                "title": "Reprocessed",
                "overview": "Updated summary",
                "emoji": "🧠",
                "category": "work",
                "action_items": [{"description": "new action"}],
                "events": [],
            },
            "memories": ["new memory"],
            "discarded": False,
        }

    async def fake_publish(_env, *, uid, source_kind, source_id):
        return None

    monkeypatch.setattr("conversation_finalization_routes._fanout_targets", fake_targets)
    monkeypatch.setattr("conversation_finalization_routes._enrichment", fake_enrichment)
    monkeypatch.setattr("developer_conversation_create_routes.publish_vector_projection", fake_publish)
    job = env.APP_DB.connection.execute(
        "SELECT job_id, finalization_revision FROM cf_conversation_finalization_jobs WHERE operation = 'reprocess'"
    ).fetchone()
    response = asyncio.run(
        process_conversation_finalization(
            FakeRequest(
                env,
                headers=auth_headers(authority="internal"),
                body={
                    "job_id": job[0],
                    "conversation_id": CONVERSATION_ID,
                    "revision": job[1],
                    "operation": "reprocess",
                    "language_code": "en",
                },
            )
        )
    )

    assert response["operation"] == "reprocess"
    assert (
        env.APP_DB.connection.execute(
            "SELECT COUNT(*) FROM cf_action_items WHERE uid = ? AND conversation_id = ? AND id = 'old-action'",
            (UID, CONVERSATION_ID),
        ).fetchone()[0]
        == 0
    )
    assert (
        env.APP_DB.connection.execute(
            "SELECT COUNT(*) FROM cf_memories WHERE uid = ? AND conversation_id = ? AND id = 'old-memory'",
            (UID, CONVERSATION_ID),
        ).fetchone()[0]
        == 0
    )
    assert (
        env.APP_DB.connection.execute(
            "SELECT COUNT(*) FROM cf_action_items WHERE uid = ? AND conversation_id = ? AND description = 'new action'",
            (UID, CONVERSATION_ID),
        ).fetchone()[0]
        == 1
    )
    assert (
        env.APP_DB.connection.execute(
            "SELECT COUNT(*) FROM cf_memories WHERE uid = ? AND conversation_id = ? AND content = 'new memory'",
            (UID, CONVERSATION_ID),
        ).fetchone()[0]
        == 1
    )
    conversation = env.APP_DB.connection.execute(
        "SELECT status, discarded, external_data_json FROM cf_conversations WHERE uid = ? AND id = ?",
        (UID, CONVERSATION_ID),
    ).fetchone()
    assert tuple(conversation) == ("completed", 0, '{"calendar_event_id":"event-1"}')
