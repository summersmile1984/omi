import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from action_item_routes import search_action_items  # noqa: E402
from tool_routes import (  # noqa: E402
    create_action_item,
    get_action_items,
    get_conversations,
    get_memories,
    search_conversation_chunks,
    search_conversations,
    search_memories,
    update_action_item,
)
from agent_tools_routes import execute_tool  # noqa: E402

SECRET = "tool-route-test-secret"
UID = "tool-user"


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
    def __init__(self, env, *, body=None, query=None, authenticated=True):
        self.scope = {"env": env}
        self.headers = signed_headers() if authenticated else {}
        self.query_params = Query(query or {})
        self._body = b"" if body is None else json.dumps(body).encode()

    async def body(self):
        return self._body


class FakeAi:
    def __init__(self):
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        return {"data": [[0.01] * 1024 for _ in payload["text"]]}


class FakeVectorIndex:
    def __init__(self):
        self.matches = []
        self.calls = []

    async def query(self, vector, options):
        self.calls.append((vector, options))
        return {"count": len(self.matches), "matches": list(self.matches)}


class FakeQueue:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


def signed_headers():
    payload = json.dumps(
        {"uid": UID, "authority": "better-auth", "requestId": "tool-route-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def environment():
    database = FakeDb()
    env = SimpleNamespace(
        APP_DB=database,
        AI=FakeAi(),
        JOBS=FakeQueue(),
        MEMORY_VECTORS=FakeVectorIndex(),
        ACTION_ITEM_VECTORS=FakeVectorIndex(),
        CONVERSATION_VECTORS=FakeVectorIndex(),
        TRANSCRIPT_CHUNK_VECTORS=FakeVectorIndex(),
        INTERNAL_ASSERTION_SECRET=SECRET,
        WORKERS_AI_VECTOR_MODEL="test-vector-model",
    )
    return database, env


def run(awaitable):
    return asyncio.run(awaitable)


def insert_fixtures(database):
    database.connection.execute(
        "INSERT INTO cf_conversations "
        "(uid,id,created_at,updated_at,started_at,finished_at,source,status,structured_json,transcript_segments_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            UID,
            "conversation-1",
            1_788_072_000,
            1_788_072_001,
            1_788_072_000,
            1_788_072_600,
            "desktop",
            "completed",
            json.dumps({"title": "Cloudflare review", "overview": "Workers migration status"}),
            json.dumps(
                [
                    {"text": "The staging deployment passed.", "is_user": True, "speaker": "SPEAKER_00"},
                    {"text": "Document the validation result.", "speaker_id": 1},
                ]
            ),
        ),
    )
    database.connection.execute(
        "INSERT INTO cf_memories "
        "(uid,id,content,category,visibility,tags_json,subject_attribution,object_entity_ids_json,qualifiers_json,"
        "uncertainty_reasons_json,conversation_id,reviewed,manually_added,memory_tier,valid_at,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            UID,
            "memory-1",
            "The user prefers Cloudflare Workers.",
            "interesting",
            "private",
            "[]",
            "user",
            "[]",
            "{}",
            "[]",
            "conversation-1",
            0,
            0,
            "short_term",
            1_788_072_000,
            1_788_072_000,
            1_788_072_001,
        ),
    )
    database.connection.execute(
        "INSERT INTO cf_action_items "
        "(uid,id,description,status,completed,owner,source,provenance_json,conversation_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            UID,
            "action-1",
            "Document staging validation",
            "active",
            0,
            "user",
            "manual",
            "[]",
            "conversation-1",
            1_788_072_000,
            1_788_072_001,
        ),
    )
    database.connection.commit()


def insert_vector_state(database, kind, source_id, vector_id, *, sub_id="000000"):
    database.connection.execute(
        "INSERT INTO cf_vector_projection_state "
        "(uid,projection_kind,source_id,sub_id,vector_id,source_version,model,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (UID, kind, source_id, sub_id, vector_id, 1_788_072_001, "test-vector-model", 1_788_072_001),
    )
    database.connection.commit()


def test_tool_lists_are_authenticated_uid_scoped_and_emit_typed_sources():
    database, env = environment()
    insert_fixtures(database)

    unauthorized = run(get_memories(FakeRequest(env, authenticated=False)))
    assert unauthorized.status_code == 401

    conversations = run(get_conversations(FakeRequest(env, query={"include_transcript": "true"})))
    assert conversations["tool_name"] == "get_conversations"
    assert "The staging deployment passed" in conversations["result_text"]
    assert conversations["sources"][0]["source_id"] == "conversation-1"

    memories = run(get_memories(FakeRequest(env)))
    assert memories["tool_name"] == "get_memories"
    assert "prefers Cloudflare Workers" in memories["result_text"]
    assert memories["sources"][0]["kind"] == "memory"

    actions = run(get_action_items(FakeRequest(env)))
    assert actions["tool_name"] == "get_action_items"
    assert "Document staging validation" in actions["result_text"]
    assert actions["sources"][0]["kind"] == "task"
    database.connection.close()


def test_tool_vector_searches_hydrate_d1_and_reconstruct_transcript_chunks():
    database, env = environment()
    insert_fixtures(database)
    memory_vector = "a" * 64
    action_vector = "b" * 64
    conversation_vector = "c" * 64
    chunk_vector = "d" * 64
    insert_vector_state(database, "memory", "memory-1", memory_vector)
    insert_vector_state(database, "action_item", "action-1", action_vector)
    insert_vector_state(database, "conversation", "conversation-1", conversation_vector)
    insert_vector_state(database, "transcript_chunk", "conversation-1", chunk_vector)
    env.MEMORY_VECTORS.matches = [{"id": memory_vector, "score": 0.91}]
    env.ACTION_ITEM_VECTORS.matches = [{"id": action_vector, "score": 0.88}]
    env.CONVERSATION_VECTORS.matches = [{"id": conversation_vector, "score": 0.86}]
    env.TRANSCRIPT_CHUNK_VECTORS.matches = [{"id": chunk_vector, "score": 0.94}]

    memories = run(search_memories(FakeRequest(env, body={"query": "deployment"})))
    assert "Found 1 memories" in memories["result_text"]
    assert memories["sources"][0]["source_id"] == "memory-1"

    actions = run(search_action_items(FakeRequest(env, query={"query": "document", "limit": "10"})))
    assert [item["id"] for item in actions["action_items"]] == ["action-1"]

    conversations = run(search_conversations(FakeRequest(env, body={"query": "staging"})))
    assert "Found 1 conversations" in conversations["result_text"]
    assert conversations["sources"][0]["source_id"] == "conversation-1"

    chunks = run(search_conversation_chunks(FakeRequest(env, body={"query": "validation"})))
    assert "Excerpt 1 (relevance: 0.94)" in chunks["result_text"]
    assert "User: The staging deployment passed." in chunks["result_text"]
    assert chunks["sources"][0]["source_id"] == "conversation-1"
    database.connection.close()


def test_tool_action_mutations_share_vector_lifecycle_and_validation_envelope():
    database, env = environment()

    invalid = run(create_action_item(FakeRequest(env, body={})))
    assert invalid.status_code == 422

    created = run(
        create_action_item(
            FakeRequest(
                env,
                body={
                    "description": "Ship the Worker search routes",
                    "due_at": "2026-09-01T12:00:00Z",
                },
            )
        )
    )
    assert created["is_error"] is False
    assert "Added" in created["result_text"]
    row = database.connection.execute(
        "SELECT id FROM cf_action_items WHERE uid = ? AND description = ?",
        (UID, "Ship the Worker search routes"),
    ).fetchone()
    item_id = row["id"]
    projection = database.connection.execute(
        "SELECT operation FROM cf_vector_projection_outbox WHERE uid = ? AND source_id = ?",
        (UID, item_id),
    ).fetchone()
    assert tuple(projection) == ("upsert",)

    updated = run(update_action_item(FakeRequest(env, body={"completed": True}), item_id))
    assert "marked as completed" in updated["result_text"]
    stored = database.connection.execute(
        "SELECT completed,status FROM cf_action_items WHERE uid = ? AND id = ?",
        (UID, item_id),
    ).fetchone()
    assert tuple(stored) == (1, "completed")
    assert [message["kind"] for message in env.JOBS.messages] == ["vector_project", "vector_project"]
    database.connection.close()


def test_agent_execute_tool_dispatches_to_cloudflare_native_handlers():
    database, env = environment()
    insert_fixtures(database)

    response = run(
        execute_tool(
            FakeRequest(
                env,
                body={"tool_name": "get_memories_tool", "params": {"limit": 1}},
            )
        )
    )
    assert response == {
        "result": "User Memories (1 total):\n\n- The user prefers Cloudflare Workers. "
        "(category: interesting, date: 2026-08-30)"
    }

    unknown = run(
        execute_tool(
            FakeRequest(
                env,
                body={"tool_name": "get_calendar_events_tool", "params": {}},
            )
        )
    )
    assert unknown.status_code == 404
    database.connection.close()
