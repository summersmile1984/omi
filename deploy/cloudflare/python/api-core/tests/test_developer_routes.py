import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from developer_routes import (  # noqa: E402
    get_developer_conversation,
    get_developer_goal,
    get_developer_goal_history,
    list_developer_action_items,
    list_developer_conversations,
    list_developer_folders,
    list_developer_goals,
    list_developer_memories,
    search_developer_memories,
)

RAW_SECRET = "0123456789abcdef0123456789abcdef"
AUTHORIZATION = f"Bearer omi_dev_{RAW_SECRET}"
READ_SCOPES = [
    "conversations:read",
    "memories:read",
    "action_items:read",
    "goals:read",
]


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


class Query(dict):
    def getlist(self, name):
        value = self.get(name)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


class FakeRequest:
    def __init__(self, env, *, query=None, authorization=AUTHORIZATION):
        self.scope = {"env": env}
        self.headers = {}
        if authorization is not None:
            self.headers["authorization"] = authorization
        self.query_params = Query(query or {})


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


def environment(*, scopes=None, cutover_state="new"):
    database = FakeDb()
    env = SimpleNamespace(
        APP_DB=database,
        AI=FakeAi(),
        MEMORY_VECTORS=FakeVectorIndex(),
        WORKERS_AI_VECTOR_MODEL="developer-vector-test-model",
    )
    digest = hashlib.sha256(RAW_SECRET.encode()).hexdigest()
    prefix = f"omi_dev_{RAW_SECRET[:4]}...{RAW_SECRET[-4:]}"
    database.connection.execute(
        "INSERT INTO cf_developer_api_keys "
        "(uid, key_id, name, key_hash, key_prefix, app_id, scopes_json, created_at) "
        "VALUES ('developer-user', 'developer-key', 'Test key', ?, ?, 'developer_api', ?, 1)",
        (digest, prefix, json.dumps(scopes if scopes is not None else READ_SCOPES)),
    )
    database.connection.execute(
        "INSERT INTO cf_account_cutover "
        "(uid, state, checkpoint_phase, destination_backend_bound, updated_at) "
        "VALUES ('developer-user', ?, 'completed', 1, 1)",
        (cutover_state,),
    )
    database.connection.commit()
    return database, env


def response_body(response):
    return json.loads(response.body)


def run(awaitable):
    return asyncio.run(awaitable)


def insert_vector_state(database, source_id, vector_id):
    database.connection.execute(
        "INSERT INTO cf_vector_projection_state "
        "(uid, projection_kind, source_id, sub_id, vector_id, source_version, model, updated_at) "
        "VALUES ('developer-user', 'memory', ?, '000000', ?, 10, 'developer-vector-test-model', 10)",
        (source_id, vector_id),
    )


def test_developer_auth_is_strict_scope_bound_and_cutover_fenced():
    database, env = environment(scopes=["memories:read"])

    missing = run(list_developer_memories(FakeRequest(env, authorization=None)))
    assert missing.status_code == 401
    malformed = run(list_developer_memories(FakeRequest(env, authorization="Bearer omi_mcp_" + RAW_SECRET)))
    assert malformed.status_code == 403
    insufficient = run(list_developer_action_items(FakeRequest(env)))
    assert insufficient.status_code == 403
    assert "action_items:read" in response_body(insufficient)["detail"]

    database.connection.execute(
        "INSERT INTO cf_account_deletion_intents "
        "(uid, job_id, status, phase, next_attempt_at, created_at, updated_at) "
        "VALUES ('developer-user', 'delete-job', 'pending', 'quiescing', 1, 1, 1)"
    )
    database.connection.commit()
    deleting = run(list_developer_memories(FakeRequest(env)))
    assert deleting.status_code == 409

    stale_database, stale_env = environment(cutover_state="legacy")
    stale = run(list_developer_memories(FakeRequest(stale_env)))
    assert stale.status_code == 409
    assert response_body(stale) == {"error": "account data plane not active"}
    stale_database.connection.close()
    database.connection.close()


def test_developer_reads_project_only_uid_scoped_unlocked_cloudflare_data():
    database, env = environment()
    connection = database.connection
    for memory_id, locked, uid in (
        ("visible-memory", 0, "developer-user"),
        ("locked-memory", 1, "developer-user"),
        ("foreign-memory", 0, "other-user"),
    ):
        connection.execute(
            "INSERT INTO cf_memories "
            "(uid, id, content, category, visibility, tags_json, reviewed, user_review, is_locked, "
            "memory_tier, valid_at, created_at, updated_at) "
            "VALUES (?, ?, ?, 'system', 'private', '[\"cloudflare\"]', 1, 1, ?, "
            "'long_term', 10, 10, 10)",
            (uid, memory_id, f"Memory {memory_id}", locked),
        )
    for item_id, locked, uid in (
        ("visible-action", 0, "developer-user"),
        ("locked-action", 1, "developer-user"),
        ("foreign-action", 0, "other-user"),
    ):
        connection.execute(
            "INSERT INTO cf_action_items "
            "(uid, id, description, status, completed, is_locked, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', 0, ?, 10, 10)",
            (uid, item_id, f"Action {item_id}", locked),
        )
    connection.execute(
        "INSERT INTO cf_folders "
        "(uid, id, name, color, icon, created_at, updated_at, display_order, conversation_count) "
        "VALUES ('developer-user', 'folder-1', 'Work', '#ffffff', 'briefcase', 10, 10, 2, 1)"
    )
    structured = json.dumps(
        {
            "title": "Cloudflare review",
            "overview": "Validate the staging data plane",
            "emoji": "✅",
            "category": "work",
            "action_items": [],
            "events": [],
        }
    )
    transcript = json.dumps([{"id": "segment-1", "text": "Deploy staging", "speaker_id": 0, "start": 1, "end": 2}])
    for conversation_id, locked, uid in (
        ("visible-conversation", 0, "developer-user"),
        ("locked-conversation", 1, "developer-user"),
        ("foreign-conversation", 0, "other-user"),
    ):
        connection.execute(
            "INSERT INTO cf_conversations "
            "(uid, id, created_at, updated_at, started_at, finished_at, source, language, status, discarded, "
            "is_locked, folder_id, structured_json, transcript_segments_json) "
            "VALUES (?, ?, 10, 10, 10, 20, 'external_integration', 'en', 'completed', 0, ?, ?, ?, ?)",
            (uid, conversation_id, locked, "folder-1" if uid == "developer-user" else None, structured, transcript),
        )
    for goal_id, active, uid in (
        ("active-goal", 1, "developer-user"),
        ("inactive-goal", 0, "developer-user"),
        ("foreign-goal", 1, "other-user"),
    ):
        connection.execute(
            "INSERT INTO cf_goals "
            "(uid, id, title, desired_outcome, status, metric_json, source, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Ship the migration', 'focused', ?, 'user', ?, 10, 10)",
            (
                uid,
                goal_id,
                f"Goal {goal_id}",
                json.dumps({"type": "numeric", "target": 10, "current": 4, "min": 0, "max": 10, "unit": "routes"}),
                active,
            ),
        )
    connection.execute(
        "INSERT INTO cf_goal_progress_history (uid, goal_id, date, value, recorded_at) "
        "VALUES ('developer-user', 'active-goal', '2026-08-30', 4, 10)"
    )
    connection.commit()

    memories = run(list_developer_memories(FakeRequest(env)))
    assert [item["id"] for item in memories] == ["visible-memory"]
    assert memories[0]["tags"] == ["cloudflare"]
    assert "uid" not in memories[0]

    action_items = run(list_developer_action_items(FakeRequest(env)))
    assert [item["id"] for item in action_items] == ["visible-action"]
    assert set(action_items[0]) == {
        "id",
        "description",
        "completed",
        "created_at",
        "updated_at",
        "due_at",
        "completed_at",
        "conversation_id",
    }

    folders = run(list_developer_folders(FakeRequest(env)))
    assert folders[0]["name"] == "Work"
    assert folders[0]["order"] == 2

    conversations = run(list_developer_conversations(FakeRequest(env)))
    assert [item["id"] for item in conversations] == ["visible-conversation"]
    assert conversations[0]["transcript_segments"] is None
    assert conversations[0]["folder_name"] == "Work"
    detail = run(
        get_developer_conversation(
            FakeRequest(env, query={"include_transcript": "true"}),
            "visible-conversation",
        )
    )
    assert detail["transcript_segments"][0]["text"] == "Deploy staging"
    assert detail["transcript_segments"][0]["speaker_name"] is None
    locked_detail = run(get_developer_conversation(FakeRequest(env), "locked-conversation"))
    assert locked_detail.status_code == 404

    goals = run(list_developer_goals(FakeRequest(env)))
    assert [goal["id"] for goal in goals] == ["active-goal"]
    all_goals = run(list_developer_goals(FakeRequest(env, query={"include_inactive": "true"})))
    assert {goal["id"] for goal in all_goals} == {"active-goal", "inactive-goal"}
    goal = run(get_developer_goal(FakeRequest(env), "inactive-goal"))
    assert goal["goal_type"] == "numeric"
    assert goal["unit"] == "routes"
    history = run(get_developer_goal_history(FakeRequest(env), "active-goal"))
    assert history == [
        {
            "date": "2026-08-30",
            "value": 4.0,
            "recorded_at": "1970-01-01T00:00:10+00:00",
        }
    ]

    last_used = connection.execute(
        "SELECT last_used_at FROM cf_developer_api_keys WHERE key_id = 'developer-key'"
    ).fetchone()[0]
    assert isinstance(last_used, int) and last_used >= 1
    connection.close()


def test_developer_memory_vector_search_hydrates_active_rows_and_keeps_legacy_twenty_item_cap():
    database, env = environment()
    visible_id = "visible-memory"
    locked_id = "locked-memory"
    for memory_id, locked in ((visible_id, 0), (locked_id, 1)):
        database.connection.execute(
            "INSERT INTO cf_memories "
            "(uid, id, content, category, reviewed, user_review, is_locked, memory_tier, valid_at, created_at, updated_at) "
            "VALUES ('developer-user', ?, ?, 'system', 1, 1, ?, 'long_term', 10, 10, 10)",
            (memory_id, f"Memory {memory_id}", locked),
        )
    visible_vector = "a" * 64
    locked_vector = "b" * 64
    insert_vector_state(database, visible_id, visible_vector)
    insert_vector_state(database, locked_id, locked_vector)
    database.connection.commit()
    env.MEMORY_VECTORS.matches = [
        {"id": locked_vector, "score": 0.99},
        {"id": visible_vector, "score": 0.87},
    ]

    result = run(search_developer_memories(FakeRequest(env, query={"query": "launch", "limit": "100"})))
    assert result["items"] == [
        {
            "id": visible_id,
            "content": "Memory visible-memory",
            "category": "system",
            "relevance_score": 0.87,
        }
    ]
    assert result["returned_count"] == 1
    assert env.MEMORY_VECTORS.calls[0][1]["topK"] == 60
    assert result["archive_default_visible"] is False

    missing_query = run(search_developer_memories(FakeRequest(env)))
    assert missing_query.status_code == 422
    database.connection.close()
