import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from user_export_routes import export_user_data  # noqa: E402


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for name in (
            "0016_action_items.sql",
            "0017_people.sql",
            "0018_goals.sql",
            "0023_goal_progress_history.sql",
            "0025_goal_progress_events.sql",
            "0026_workstreams.sql",
            "0032_conversations.sql",
            "0037_memories.sql",
            "0042_chat_messages.sql",
        ):
            self.connection.executescript((migration_dir / name).read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeStatement:
    def __init__(self, connection, sql):
        self.connection = connection
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def all(self):
        rows = self.connection.execute(self.sql, self.args).fetchall()
        return {"results": [dict(row) for row in rows]}


class FakeProfileResponse:
    status = 200

    async def json(self):
        return {"uid": "export-user", "name": "Export User", "email": "export@example.com"}


class FakeAuth:
    async def fetch(self, url, **kwargs):
        assert url.endswith("/internal/profile")
        assert kwargs["headers"]["x-omi-auth-context"]
        return FakeProfileResponse()


class FakeRequest:
    def __init__(self, env, headers):
        self.scope = {"env": env}
        self.headers = headers


def signed_headers(secret: str, uid: str = "export-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "export-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(secret: str):
    return type(
        "Env",
        (),
        {
            "APP_DB": FakeDb(),
            "INTERNAL_ASSERTION_SECRET": secret,
            "AUTH": FakeAuth(),
        },
    )()


def _insert_fixtures(env):
    db = env.APP_DB.connection
    db.execute(
        "INSERT INTO cf_conversations (uid, id, created_at, structured_json, transcript_segments_json) VALUES (?, ?, ?, ?, ?)",
        ("export-user", "conversation-1", 10, '{"title":"A meeting"}', '[{"text":"hello"}]'),
    )
    db.execute(
        "INSERT INTO cf_conversations (uid, id, created_at, structured_json) VALUES (?, ?, ?, ?)",
        ("other-user", "other-conversation", 20, '{"title":"private"}'),
    )
    db.execute(
        "INSERT INTO cf_memories (uid, id, content, tags_json, arguments_json, object_entity_ids_json, qualifiers_json, uncertainty_reasons_json, capture_device_ids_json, memory_tier, valid_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "export-user",
            "memory-1",
            "Remember this",
            '["one"]',
            '{"place":"home"}',
            '["entity-1"]',
            '{}',
            '[]',
            '[]',
            "long_term",
            10,
            10,
            10,
        ),
    )
    db.execute(
        "INSERT INTO cf_people (uid, id, name, speech_samples_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("export-user", "person-1", "Alex", '["sample"]', 10, 10),
    )
    db.execute(
        "INSERT INTO cf_action_items (uid, id, description, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("export-user", "action-1", "Send follow-up", "active", 10, 10),
    )
    db.execute(
        "INSERT INTO cf_goals (uid, id, title, desired_outcome, status, source, success_criteria_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("export-user", "goal-1", "Ship export", "A working export", "focused", "user", '["download"]', 10, 10),
    )
    db.execute(
        "INSERT INTO cf_goal_progress_history (uid, goal_id, date, value, recorded_at) VALUES (?, ?, ?, ?, ?)",
        ("export-user", "goal-1", "2026-08-30", 0.5, 11),
    )
    db.execute(
        "INSERT INTO cf_goal_progress_events (uid, event_id, goal_id, sequence, kind, summary, evidence_refs_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("export-user", "goal-event-1", "goal-1", 1, "evidence", "Tested export", '["conversation-1"]', 11),
    )
    db.execute(
        "INSERT INTO cf_workstreams (uid, id, title, objective, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("export-user", "stream-1", "Export stream", "Finish route", "open", 10, 11),
    )
    db.execute(
        "INSERT INTO cf_workstream_events (uid, event_id, workstream_id, sequence, kind, summary, evidence_refs_json, sensitivity, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("export-user", "stream-event-1", "stream-1", 1, "decision", "Use D1", '["goal-1"]', "normal", 11),
    )
    db.execute(
        "INSERT INTO cf_workstream_artifacts (uid, artifact_id, workstream_id, logical_key, version, kind, uri, content_hash, evidence_event_ids_json, evidence_refs_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "export-user",
            "artifact-1",
            "stream-1",
            "route",
            1,
            "document",
            "r2://route",
            "1234567890abcdef",
            '[]',
            '[]',
            "draft",
            11,
        ),
    )
    db.execute(
        "INSERT INTO cf_workstream_checkpoints (uid, checkpoint_id, workstream_id, runtime_id, last_event_sequence, context_summary, evidence_refs_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("export-user", "checkpoint-1", "stream-1", "runtime-1", 1, "Ready", '[]', 11),
    )
    db.execute(
        "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) VALUES (?, ?, ?, ?, ?)",
        ("export-user", "chat-1", "app-1", 10, '{"role":"user","content":"hello"}'),
    )
    db.commit()


def _body(response):
    return json.loads(response.body.decode())


def test_export_is_authenticated_uid_scoped_and_preserves_user_visible_shape():
    secret = "export-secret"
    env = make_env(secret)
    _insert_fixtures(env)

    response = asyncio.run(export_user_data(FakeRequest(env, signed_headers(secret))))

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="omi-export.json"'
    payload = _body(response)
    assert payload["profile"] == {"uid": "export-user", "name": "Export User", "email": "export@example.com"}
    assert len(payload["conversations"]) == 1
    assert payload["conversations"][0]["structured"] == {"title": "A meeting"}
    assert "structured_json" not in payload["conversations"][0]
    assert payload["memories"][0]["tags"] == ["one"]
    assert "uid" not in payload["memories"][0]
    assert payload["action_items"][0]["id"] == "action-1"
    assert payload["task_data"]["goals"][0]["title"] == "Ship export"
    assert payload["task_data"]["goal_history"][0]["value"] == 0.5
    assert payload["task_data"]["goal_events"][0]["evidence_refs"] == ["conversation-1"]
    assert payload["task_data"]["workstream_artifact_refs"][0]["artifact_id"] == "artifact-1"
    assert payload["task_data"]["workstream_continuation_checkpoints"][0]["runtime_id"] == "runtime-1"
    assert payload["chat_messages"][0]["message"] == {"role": "user", "content": "hello"}
    assert payload["task_data"]["candidates"] == []
    assert isinstance(payload["exported_at"], int)


def test_export_rejects_missing_auth_and_converts_d1_failures_to_503():
    secret = "export-secret"
    env = make_env(secret)
    assert asyncio.run(export_user_data(FakeRequest(env, {}))).status_code == 401

    broken = type("Env", (), {"APP_DB": None, "INTERNAL_ASSERTION_SECRET": secret})()
    response = asyncio.run(export_user_data(FakeRequest(broken, signed_headers(secret))))
    assert response.status_code == 503
    assert _body(response) == {"error": "user export unavailable"}
