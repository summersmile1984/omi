import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from integration_routes import (  # noqa: E402
    create_conversation,
    create_memories,
    list_conversations,
    list_memories,
    list_tasks,
    search_conversations,
    send_notification,
    send_notification_v1,
)


class FakeStatement:
    def __init__(self, connection, sql, args=()):
        self.connection = connection
        self.sql = sql
        self.args = args

    def bind(self, *args):
        return FakeStatement(self.connection, self.sql, args)

    async def first(self):
        cursor = self.connection.execute(self.sql, self.args)
        row = cursor.fetchone()
        self.connection.commit()
        return dict(row) if row is not None else None

    async def all(self):
        rows = self.connection.execute(self.sql, self.args).fetchall()
        return {"results": [dict(row) for row in rows]}

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}

    def execute(self):
        cursor = self.connection.execute(self.sql, self.args)
        rows = cursor.fetchall() if cursor.description else []
        return {"results": [dict(row) for row in rows], "meta": {"changes": cursor.rowcount}}


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
            results = [statement.execute() for statement in statements]
            self.connection.commit()
            return results
        except Exception:
            self.connection.rollback()
            raise


class Query(dict):
    def getlist(self, name):
        value = self.get(name)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


class FakeRequest:
    def __init__(self, env, *, body=None, query=None, authorization="Bearer sk_integrationsecret"):
        self.scope = {"env": env}
        self.headers = {"authorization": authorization} if authorization is not None else {}
        self.query_params = Query(query or {})
        self._body = b"" if body is None else json.dumps(body).encode()

    async def body(self):
        return self._body


class FakeAi:
    def __init__(self):
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        assert payload["response_format"]["type"] == "json_schema"
        prompt = payload["messages"][1]["content"]
        if prompt.startswith("Extract durable"):
            return {"response": json.dumps({"memories": ["The user prefers tea."]})}
        return {
            "response": {
                "title": "Tea planning",
                "overview": "The user planned a tea tasting.",
                "emoji": "🍵",
                "category": "other",
                "action_items": [{"description": "Buy green tea"}],
            }
        }


def environment():
    db = FakeDb()
    ai = FakeAi()
    env = type(
        "Env",
        (),
        {
            "APP_DB": db,
            "AI": ai,
            "WORKERS_AI_INTEGRATION_MODEL": "test-model",
        },
    )()
    payload = {
        "id": "integration-app",
        "name": "Integration App",
        "capabilities": ["external_integration"],
        "external_integration": {
            "actions": [
                {"action": "create_conversation"},
                {"action": "create_memories"},
                {"action": "read_memories"},
                {"action": "read_conversations"},
                {"action": "read_tasks"},
            ],
            "chat_messages_enabled": True,
            "chat_messages_target": "main",
        },
    }
    db.connection.execute(
        "INSERT INTO cf_app_catalog "
        "(id, approved, status, disabled, data_json, updated_at, owner_uid) "
        "VALUES ('integration-app', 1, 'approved', 0, ?, 1, 'owner-user')",
        (json.dumps(payload),),
    )
    webhook_payload = {
        "id": "webhook-app",
        "name": "Webhook App",
        "capabilities": ["external_integration"],
        "external_integration": {
            "triggers_on": "memory_creation",
            "webhook_url": "https://hooks.example.test/conversation",
            "actions": [],
        },
    }
    db.connection.execute(
        "INSERT INTO cf_app_catalog "
        "(id, approved, status, disabled, data_json, updated_at, owner_uid) "
        "VALUES ('webhook-app', 1, 'approved', 0, ?, 1, 'webhook-owner')",
        (json.dumps(webhook_payload),),
    )
    for uid in ("integration-user", "v1-user"):
        db.connection.execute(
            "INSERT INTO cf_user_enabled_apps (uid, app_id, created_at) VALUES (?, 'integration-app', 1)",
            (uid,),
        )
    db.connection.execute(
        "INSERT INTO cf_user_enabled_apps (uid, app_id, created_at) " "VALUES ('integration-user', 'webhook-app', 1)"
    )
    raw_key = "integrationsecret"
    db.connection.execute(
        "INSERT INTO cf_app_api_keys (app_id, key_id, key_hash, label, created_at) VALUES (?, ?, ?, ?, 1)",
        (
            "integration-app",
            "key-1",
            hashlib.sha256(raw_key.encode()).hexdigest(),
            "sk_inte...cret",
        ),
    )
    db.connection.commit()
    return db, ai, env


def response_json(response):
    return json.loads(response.body)


def test_integration_writes_use_workers_ai_and_persist_canonical_d1_projections():
    db, ai, env = environment()
    created = asyncio.run(
        create_conversation(
            FakeRequest(
                env,
                query={"uid": "integration-user"},
                body={
                    "text": "We should host a tea tasting and buy green tea.",
                    "language": "en",
                    "text_source": "other",
                },
            ),
            "integration-app",
        )
    )
    assert created == {}
    conversation = db.connection.execute(
        "SELECT source, app_id, structured_json, transcript_segments_json FROM cf_conversations"
    ).fetchone()
    assert conversation["source"] == "external_integration"
    assert conversation["app_id"] == "integration-app"
    assert json.loads(conversation["structured_json"])["title"] == "Tea planning"
    assert json.loads(conversation["transcript_segments_json"])[0]["text"].startswith("We should")
    assert db.connection.execute("SELECT description FROM cf_action_items").fetchone()[0] == "Buy green tea"
    fanout = db.connection.execute(
        "SELECT app_id, webhook_url, payload_json, status FROM cf_integration_webhook_outbox"
    ).fetchone()
    assert fanout["app_id"] == "webhook-app"
    assert fanout["webhook_url"] == "https://hooks.example.test/conversation"
    assert json.loads(fanout["payload_json"])["id"]
    assert fanout["status"] == "pending"

    memories = asyncio.run(
        create_memories(
            FakeRequest(
                env,
                query={"uid": "integration-user"},
                body={
                    "text": "I prefer tea.",
                    "text_source": "other",
                    "memories": [{"content": "The user owns a teapot.", "tags": ["tea"]}],
                },
            ),
            "integration-app",
        )
    )
    assert memories == {}
    stored = db.connection.execute(
        "SELECT content, app_id, qualifiers_json FROM cf_memories ORDER BY content"
    ).fetchall()
    assert [row["content"] for row in stored] == ["The user owns a teapot.", "The user prefers tea."]
    assert all(row["app_id"] == "integration-app" for row in stored)
    assert all("integration" in json.loads(row["qualifiers_json"]) for row in stored)
    assert len(ai.calls) == 2
    assert (
        db.connection.execute(
            "SELECT COUNT(*) FROM cf_integration_hourly_usage WHERE uid = 'integration-user'"
        ).fetchone()[0]
        == 2
    )


def test_integration_reads_enforce_app_key_installation_capabilities_and_redaction():
    db, _, env = environment()
    asyncio.run(
        create_conversation(
            FakeRequest(
                env,
                query={"uid": "integration-user"},
                body={"text": "A searchable tea conversation."},
            ),
            "integration-app",
        )
    )
    asyncio.run(
        create_memories(
            FakeRequest(
                env,
                query={"uid": "integration-user"},
                body={"memories": [{"content": "x" * 100}]},
            ),
            "integration-app",
        )
    )
    db.connection.execute("UPDATE cf_memories SET is_locked = 1")
    db.connection.execute("UPDATE cf_action_items SET is_locked = 1, description = ?", ("y" * 100,))
    db.connection.commit()

    memory_result = asyncio.run(
        list_memories(
            FakeRequest(env, query={"uid": "integration-user"}),
            "integration-app",
        )
    )
    assert memory_result["memories"][0]["content"] == "x" * 70 + "..."

    conversations = asyncio.run(
        list_conversations(
            FakeRequest(
                env,
                query={"uid": "integration-user", "max_transcript_segments": "1"},
            ),
            "integration-app",
        )
    )
    assert conversations["conversations"][0]["app_id"] == "integration-app"
    assert len(conversations["conversations"][0]["transcript_segments"]) == 1

    no_segments = asyncio.run(
        list_conversations(
            FakeRequest(
                env,
                query={"uid": "integration-user", "max_transcript_segments": "0"},
            ),
            "integration-app",
        )
    )
    assert no_segments["conversations"][0]["transcript_segments"] == []

    searched = asyncio.run(
        search_conversations(
            FakeRequest(
                env,
                query={"uid": "integration-user", "max_transcript_segments": "1"},
                body={"query": "searchable", "include_discarded": False},
            ),
            "integration-app",
        )
    )
    assert searched["total_pages"] == 1
    assert searched["conversations"][0]["structured"]["title"] == "Tea planning"

    tasks = asyncio.run(list_tasks(FakeRequest(env, query={"uid": "integration-user"}), "integration-app"))
    assert tasks["tasks"][0]["description"] == "y" * 70 + "..."

    invalid = asyncio.run(
        list_tasks(
            FakeRequest(env, query={"uid": "integration-user"}, authorization="Bearer sk_wrong"),
            "integration-app",
        )
    )
    assert invalid.status_code == 403
    assert response_json(invalid) == {"detail": "Invalid API key"}

    missing_auth = asyncio.run(
        list_tasks(
            FakeRequest(env, query={"uid": "integration-user"}, authorization=None),
            "integration-app",
        )
    )
    assert missing_auth.status_code == 401

    not_installed = asyncio.run(list_tasks(FakeRequest(env, query={"uid": "stranger"}), "integration-app"))
    assert not_installed.status_code == 403


def test_integration_notifications_share_outbox_chat_and_durable_hourly_limit():
    db, _, env = environment()
    for index in range(10):
        response = asyncio.run(
            send_notification(
                FakeRequest(
                    env,
                    query={"uid": "integration-user", "message": f"message {index}"},
                ),
                "integration-app",
            )
        )
        assert response.status_code == 200
        assert response.headers["x-ratelimit-remaining"] == str(9 - index)
    limited = asyncio.run(
        send_notification(
            FakeRequest(env, query={"uid": "integration-user", "message": "one too many"}),
            "integration-app",
        )
    )
    assert limited.status_code == 429
    assert limited.headers["retry-after"]
    assert (
        db.connection.execute(
            "SELECT COUNT(*) FROM cf_notification_outbox WHERE source_kind = 'integration'"
        ).fetchone()[0]
        == 10
    )
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_sessions").fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_messages").fetchone()[0] == 10
    main_message = json.loads(db.connection.execute("SELECT message_json FROM cf_chat_messages LIMIT 1").fetchone()[0])
    assert main_message["text"].startswith("[Integration App]:")
    assert main_message["from_external_integration"] is True

    v1 = asyncio.run(
        send_notification_v1(
            FakeRequest(
                env,
                body={"aid": "integration-app", "uid": "v1-user", "message": "v1 message"},
            )
        )
    )
    assert v1.status_code == 200
    assert response_json(v1) == {"status": "Ok"}
