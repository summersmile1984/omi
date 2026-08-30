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
from developer_mutation_routes import (  # noqa: E402
    create_developer_action_item,
    create_developer_action_items_batch,
    create_developer_goal,
    create_developer_memories_batch,
    create_developer_memory,
    delete_developer_action_item,
    delete_developer_conversation,
    delete_developer_goal,
    delete_developer_memory,
    update_developer_action_item,
    update_developer_conversation,
    update_developer_goal,
    update_developer_goal_progress,
    update_developer_memory,
)
from developer_conversation_create_routes import (  # noqa: E402
    create_developer_conversation,
    create_developer_conversation_from_segments,
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

    def execute(self):
        cursor = self.connection.execute(self.sql, self.args)
        rows = cursor.fetchall() if cursor.description else []
        return {"results": [dict(row) for row in rows], "meta": {"changes": cursor.rowcount}}


class FakeDb:
    def __init__(self):
        self.batch_calls = []
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for migration in sorted(migration_dir.glob("*.sql")):
            self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        self.batch_calls.append(len(statements))
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
    def __init__(self, env, *, query=None, authorization=AUTHORIZATION, body=None):
        self.scope = {"env": env}
        self.headers = {}
        if authorization is not None:
            self.headers["authorization"] = authorization
        self.query_params = Query(query or {})
        self._body = b"" if body is None else json.dumps(body).encode()

    async def body(self):
        return self._body


class FakeAi:
    def __init__(self):
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        if "messages" in payload:
            return {"response": {"category": "system"}}
        return {"data": [[0.01] * 1024 for _ in payload["text"]]}


class ConversationCreationAi:
    def __init__(self, *, valid=True, discarded=False):
        self.calls = []
        self.valid = valid
        self.discarded = discarded

    async def run(self, model, payload):
        self.calls.append((model, payload))
        if not self.valid:
            return {"response": {"title": "Incomplete"}}
        return {
            "response": {
                "title": "Cloudflare rollout",
                "overview": "The team will verify the Worker-native Developer conversation pipeline.",
                "emoji": "☁️",
                "category": "technology",
                "discarded": self.discarded,
                "action_items": [{"description": "Deploy staging"}, {"description": "Deploy staging"}],
                "memories": ["The user prefers Cloudflare Workers", "The user prefers Cloudflare Workers"],
            }
        }


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


def environment(*, scopes=None, cutover_state="new"):
    database = FakeDb()
    env = SimpleNamespace(
        APP_DB=database,
        AI=FakeAi(),
        MEMORY_VECTORS=FakeVectorIndex(),
        JOBS=FakeQueue(),
        WORKERS_AI_INTEGRATION_MODEL="developer-category-test-model",
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


def test_developer_memory_mutations_use_write_scope_d1_and_vector_outbox():
    database, env = environment(scopes=READ_SCOPES + ["memories:write"])

    created = run(
        create_developer_memory(
            FakeRequest(
                env,
                body={
                    "content": "Cloudflare owns the Developer memory path",
                    "visibility": "private",
                    "tags": ["cloudflare"],
                },
            )
        )
    )
    assert created["category"] == "system"
    assert created["manually_added"] is True
    memory_id = created["id"]

    batch = run(
        create_developer_memories_batch(
            FakeRequest(
                env,
                body={
                    "memories": [
                        {"content": "First batch memory", "category": "manual"},
                        {"content": "Second batch memory", "category": "interesting"},
                    ]
                },
            )
        )
    )
    assert batch["created_count"] == 2
    assert {item["category"] for item in batch["memories"]} == {"manual", "interesting"}

    updated = run(
        update_developer_memory(
            FakeRequest(
                env,
                body={
                    "content": "Cloudflare owns the updated memory path",
                    "visibility": "public",
                    "tags": ["updated"],
                    "category": "interesting",
                },
            ),
            memory_id,
        )
    )
    assert updated["content"] == "Cloudflare owns the updated memory path"
    assert updated["visibility"] == "public"
    assert updated["tags"] == ["updated"]
    assert updated["edited"] is True

    deleted = run(delete_developer_memory(FakeRequest(env), memory_id))
    assert deleted == {"success": True}
    missing = run(delete_developer_memory(FakeRequest(env), memory_id))
    assert missing.status_code == 404
    outbox = database.connection.execute(
        "SELECT operation FROM cf_vector_projection_outbox "
        "WHERE uid = 'developer-user' AND source_kind = 'memory' AND source_id = ?",
        (memory_id,),
    ).fetchone()
    assert dict(outbox) == {"operation": "delete"}
    assert len(env.JOBS.messages) == 5

    read_only_database, read_only_env = environment()
    denied = run(
        create_developer_memory(FakeRequest(read_only_env, body={"content": "Must not persist", "category": "manual"}))
    )
    assert denied.status_code == 403
    assert read_only_database.connection.execute("SELECT COUNT(*) FROM cf_memories").fetchone()[0] == 0
    read_only_database.connection.close()
    database.connection.close()


def test_developer_action_item_mutations_preserve_projection_lock_and_scope_contracts():
    database, env = environment(scopes=READ_SCOPES + ["action_items:write"])

    created = run(
        create_developer_action_item(
            FakeRequest(
                env,
                body={
                    "description": "Deploy Developer API writes",
                    "due_at": "2026-09-01T00:00:00Z",
                },
            )
        )
    )
    assert created["description"] == "Deploy Developer API writes"
    assert created["completed"] is False
    action_item_id = created["id"]

    duplicate = run(create_developer_action_item(FakeRequest(env, body={"description": "Deploy Developer API writes"})))
    assert duplicate["id"] != action_item_id
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM cf_action_items WHERE description = 'Deploy Developer API writes'"
        ).fetchone()[0]
        == 2
    )

    batch = run(
        create_developer_action_items_batch(
            FakeRequest(
                env,
                body={
                    "action_items": [
                        {"description": "Verify staging"},
                        {"description": "Record evidence", "completed": True},
                    ]
                },
            )
        )
    )
    assert batch["created_count"] == 2
    assert [item["completed"] for item in batch["action_items"]] == [False, True]
    assert database.batch_calls[-1] == 4

    updated = run(
        update_developer_action_item(
            FakeRequest(env, body={"description": "Deploy and verify staging", "completed": True}),
            action_item_id,
        )
    )
    assert updated["description"] == "Deploy and verify staging"
    assert updated["completed"] is True
    assert updated["completed_at"] is not None

    database.connection.execute(
        "INSERT INTO cf_action_items "
        "(uid, id, description, status, completed, is_locked, created_at, updated_at) "
        "VALUES ('developer-user', 'locked-action', 'Locked', 'active', 0, 1, 1, 1)"
    )
    database.connection.commit()
    locked = run(delete_developer_action_item(FakeRequest(env), "locked-action"))
    assert locked.status_code == 402

    deleted = run(delete_developer_action_item(FakeRequest(env), action_item_id))
    assert deleted == {"success": True}
    outbox = database.connection.execute(
        "SELECT operation FROM cf_vector_projection_outbox "
        "WHERE uid = 'developer-user' AND source_kind = 'action_item' AND source_id = ?",
        (action_item_id,),
    ).fetchone()
    assert dict(outbox) == {"operation": "delete"}

    read_only_database, read_only_env = environment()
    denied = run(create_developer_action_item(FakeRequest(read_only_env, body={"description": "Must not persist"})))
    assert denied.status_code == 403
    assert read_only_database.connection.execute("SELECT COUNT(*) FROM cf_action_items").fetchone()[0] == 0
    read_only_database.connection.close()
    database.connection.close()


def test_developer_conversation_mutations_preserve_lock_projection_and_vector_contracts():
    database, env = environment(scopes=READ_SCOPES + ["conversations:write"])
    structured = json.dumps(
        {
            "title": "Original title",
            "overview": "Cloudflare conversation",
            "emoji": "✅",
            "category": "work",
            "action_items": [],
            "events": [],
        }
    )
    database.connection.execute(
        "INSERT INTO cf_conversations "
        "(uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
        "discarded, is_locked, structured_json, transcript_segments_json) "
        "VALUES ('developer-user', 'conversation-write', 10, 10, 10, 20, 'external_integration', 'en', "
        "'completed', 'private', 0, 0, ?, '[]')",
        (structured,),
    )
    database.connection.execute(
        "INSERT INTO cf_conversations "
        "(uid, id, created_at, updated_at, source, status, is_locked, structured_json, transcript_segments_json) "
        "VALUES ('developer-user', 'locked-conversation-write', 10, 10, 'external_integration', 'completed', 1, ?, '[]')",
        (structured,),
    )
    database.connection.commit()

    discarded = run(
        update_developer_conversation(
            FakeRequest(env, body={"title": "Updated title", "discarded": True}),
            "conversation-write",
        )
    )
    assert discarded["structured"]["title"] == "Updated title"
    assert (
        database.connection.execute(
            "SELECT discarded FROM cf_conversations WHERE uid = 'developer-user' AND id = 'conversation-write'"
        ).fetchone()[0]
        == 1
    )
    assert (
        database.connection.execute(
            "SELECT operation FROM cf_vector_projection_outbox "
            "WHERE uid = 'developer-user' AND source_kind = 'conversation' AND source_id = 'conversation-write'"
        ).fetchone()[0]
        == "delete"
    )

    restored = run(
        update_developer_conversation(
            FakeRequest(env, body={"discarded": False}),
            "conversation-write",
        )
    )
    assert restored["structured"]["title"] == "Updated title"
    assert (
        database.connection.execute(
            "SELECT operation FROM cf_vector_projection_outbox "
            "WHERE uid = 'developer-user' AND source_kind = 'conversation' AND source_id = 'conversation-write'"
        ).fetchone()[0]
        == "upsert"
    )

    locked = run(delete_developer_conversation(FakeRequest(env), "locked-conversation-write"))
    assert locked.status_code == 402
    deleted = run(delete_developer_conversation(FakeRequest(env), "conversation-write"))
    assert deleted == {"success": True}
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM cf_conversations WHERE uid = 'developer-user' AND id = 'conversation-write'"
        ).fetchone()[0]
        == 0
    )

    read_only_database, read_only_env = environment()
    denied = run(
        update_developer_conversation(
            FakeRequest(read_only_env, body={"title": "Denied"}),
            "missing",
        )
    )
    assert denied.status_code == 403
    read_only_database.connection.close()
    database.connection.close()


def test_developer_goal_mutations_preserve_legacy_projection_progress_and_scope_contracts():
    database, env = environment(scopes=READ_SCOPES + ["goals:write"])

    created = run(
        create_developer_goal(
            FakeRequest(
                env,
                body={
                    "title": "Migrate Developer goals",
                    "desired_outcome": "Cloudflare owns goal mutations",
                    "success_criteria": ["CRUD verified"],
                    "goal_type": "numeric",
                    "target_value": 10,
                    "current_value": 2,
                    "unit": "routes",
                },
            )
        )
    )
    goal_id = created["id"]
    assert created["goal_id"] == goal_id
    assert created["goal_type"] == "numeric"
    assert created["current_value"] == 2

    updated = run(
        update_developer_goal(
            FakeRequest(
                env,
                body={
                    "title": "Migrate and verify Developer goals",
                    "why_it_matters": "Reduce deployment complexity",
                    "target_value": 12,
                },
            ),
            goal_id,
        )
    )
    assert updated["title"] == "Migrate and verify Developer goals"
    assert updated["why_it_matters"] == "Reduce deployment complexity"
    assert updated["target_value"] == 12

    progressed = run(
        update_developer_goal_progress(
            FakeRequest(env, query={"current_value": "7"}),
            goal_id,
        )
    )
    assert progressed["current_value"] == 7
    history = database.connection.execute(
        "SELECT value FROM cf_goal_progress_history WHERE uid = 'developer-user' AND goal_id = ?",
        (goal_id,),
    ).fetchone()
    assert history[0] == 7

    deleted = run(delete_developer_goal(FakeRequest(env), goal_id))
    assert deleted == {"success": True}
    goal_state = database.connection.execute(
        "SELECT status, is_active FROM cf_goals WHERE uid = 'developer-user' AND id = ?",
        (goal_id,),
    ).fetchone()
    assert tuple(goal_state) == ("abandoned", 0)

    read_only_database, read_only_env = environment()
    denied = run(create_developer_goal(FakeRequest(read_only_env, body={"title": "Denied"})))
    assert denied.status_code == 403
    assert read_only_database.connection.execute("SELECT COUNT(*) FROM cf_goals").fetchone()[0] == 0
    read_only_database.connection.close()
    database.connection.close()


def _enable_creation_fanout(database):
    database.connection.execute(
        "INSERT INTO cf_user_developer_webhooks "
        "(uid, webhook_type, url, enabled, created_at, updated_at) "
        "VALUES ('developer-user', 'memory_created', 'https://developer.example/hook?token=kept', 1, 1, 1)"
    )
    database.connection.execute(
        "INSERT INTO cf_app_catalog "
        "(id, approved, status, disabled, data_json, updated_at) VALUES (?, 1, 'approved', 0, ?, 1)",
        (
            "fanout-app",
            json.dumps(
                {
                    "id": "fanout-app",
                    "name": "Fanout",
                    "capabilities": ["external_integration"],
                    "external_integration": {
                        "triggers_on": "memory_creation",
                        "webhook_url": "https://app.example/hook",
                    },
                    "is_paid": False,
                }
            ),
        ),
    )
    database.connection.execute(
        "INSERT INTO cf_user_enabled_apps (uid, app_id, created_at) " "VALUES ('developer-user', 'fanout-app', 1)"
    )
    database.connection.commit()


def test_developer_text_conversation_creation_commits_all_worker_native_effects():
    database, env = environment(scopes=READ_SCOPES + ["conversations:write"])
    env.AI = ConversationCreationAi()
    _enable_creation_fanout(database)

    result = run(
        create_developer_conversation(
            FakeRequest(
                env,
                body={
                    "text": "We should deploy the Cloudflare staging service and validate it.",
                    "text_source": "message",
                    "text_source_spec": "test-suite",
                    "started_at": "2026-08-30T10:00:00Z",
                    "finished_at": "2026-08-30T10:05:00Z",
                    "language": "en",
                    "geolocation": {"latitude": 31.2304, "longitude": 121.4737},
                },
            )
        )
    )

    assert result["status"] == "completed"
    assert result["discarded"] is False
    conversation_id = result["id"]
    conversation = database.connection.execute(
        "SELECT status, source, structured_json, transcript_segments_json, external_data_json "
        "FROM cf_conversations WHERE uid = 'developer-user' AND id = ?",
        (conversation_id,),
    ).fetchone()
    assert tuple(conversation[:2]) == ("completed", "external_integration")
    assert json.loads(conversation[2])["title"] == "Cloudflare rollout"
    assert json.loads(conversation[3])[0]["text"].startswith("We should deploy")
    assert json.loads(conversation[4])["developer_api"]["text_source"] == "message"
    assert database.connection.execute("SELECT COUNT(*) FROM cf_action_items").fetchone()[0] == 1
    assert database.connection.execute("SELECT COUNT(*) FROM cf_memories").fetchone()[0] == 1
    usage = database.connection.execute(
        "SELECT transcription_seconds, memories_created FROM cf_usage_sources "
        "WHERE uid = 'developer-user' AND source_kind = 'conversation' AND source_id = ?",
        (conversation_id,),
    ).fetchone()
    assert tuple(usage) == (300, 0)
    assert database.connection.execute("SELECT COUNT(*) FROM cf_vector_projection_outbox").fetchone()[0] == 3
    assert database.connection.execute("SELECT COUNT(*) FROM cf_integration_webhook_outbox").fetchone()[0] == 1
    developer_webhook = database.connection.execute(
        "SELECT webhook_url, payload_json FROM cf_developer_webhook_outbox"
    ).fetchone()
    assert developer_webhook[0] == "https://developer.example/hook?token=kept"
    assert json.loads(developer_webhook[1])["id"] == conversation_id
    assert {message["payload"]["sourceKind"] for message in env.JOBS.messages} == {
        "conversation",
        "action_item",
        "memory",
    }
    database.connection.close()


def test_developer_from_segments_is_deterministic_idempotent_and_meeting_eligible():
    database, env = environment(scopes=READ_SCOPES + ["conversations:write"])
    env.AI = ConversationCreationAi()
    body = {
        "transcript_segments": [
            {"text": "Opening", "speaker": "SPEAKER_00", "is_user": True, "start": 0, "end": 35},
            {"text": "Cloudflare plan", "speaker": "SPEAKER_01", "start": 35, "end": 75},
        ],
        "client_conversation_id": "stable-session",
        "source": "desktop",
        "started_at": "2026-08-30T10:00:00Z",
        "finished_at": "2026-08-30T10:10:00Z",
        "conversation_role": "meeting",
        "conversation_finalization_reason": "meeting_ended",
    }

    first = run(create_developer_conversation_from_segments(FakeRequest(env, body=body)))
    second = run(create_developer_conversation_from_segments(FakeRequest(env, body=body)))

    expected_id = str(
        __import__("uuid").uuid5(
            __import__("uuid").UUID("fb2f1f36-3c84-47a4-9c62-b3f6fdb3fd13"),
            "developer-user\0stable-session",
        )
    )
    assert first == second
    assert first == {
        "id": expected_id,
        "status": "completed",
        "discarded": False,
        "meeting_treatment_eligible": True,
    }
    assert len(env.AI.calls) == 1
    assert database.connection.execute("SELECT COUNT(*) FROM cf_conversations").fetchone()[0] == 1
    assert database.connection.execute("SELECT COUNT(*) FROM cf_action_items").fetchone()[0] == 1
    assert database.connection.execute("SELECT COUNT(*) FROM cf_memories").fetchone()[0] == 1
    database.connection.close()


def test_developer_conversation_creation_fails_closed_and_removes_processing_claim():
    database, env = environment(scopes=READ_SCOPES + ["conversations:write"])
    env.AI = ConversationCreationAi(valid=False)
    body = {
        "transcript_segments": [{"text": "Retryable", "start": 0, "end": 4}],
        "client_session_id": "failed-session",
    }

    failed = run(create_developer_conversation_from_segments(FakeRequest(env, body=body)))
    assert failed.status_code == 502
    assert response_body(failed) == {"error": "conversation processing unavailable"}
    assert database.connection.execute("SELECT COUNT(*) FROM cf_conversations").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM cf_action_items").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM cf_memories").fetchone()[0] == 0

    env.AI = ConversationCreationAi()
    database.connection.execute("DROP TABLE cf_vector_projection_outbox")
    database.connection.commit()
    unavailable = run(create_developer_conversation_from_segments(FakeRequest(env, body=body)))
    assert unavailable.status_code == 503
    assert response_body(unavailable) == {"error": "conversations unavailable"}
    assert database.connection.execute("SELECT COUNT(*) FROM cf_conversations").fetchone()[0] == 0
    database.connection.close()


def test_developer_discarded_conversation_skips_derived_rows_but_keeps_creation_webhook():
    database, env = environment(scopes=READ_SCOPES + ["conversations:write"])
    env.AI = ConversationCreationAi(discarded=True)
    _enable_creation_fanout(database)

    result = run(create_developer_conversation(FakeRequest(env, body={"text": "background noise"})))

    assert result["discarded"] is True
    assert database.connection.execute("SELECT discarded FROM cf_conversations").fetchone()[0] == 1
    assert database.connection.execute("SELECT COUNT(*) FROM cf_action_items").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM cf_memories").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM cf_integration_webhook_outbox").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM cf_developer_webhook_outbox").fetchone()[0] == 1
    projection = database.connection.execute(
        "SELECT source_kind, operation FROM cf_vector_projection_outbox"
    ).fetchone()
    assert tuple(projection) == ("conversation", "delete")
    database.connection.close()


def test_developer_conversation_creation_validates_scope_and_segment_intervals():
    read_only_database, read_only_env = environment()
    denied = run(create_developer_conversation(FakeRequest(read_only_env, body={"text": "Must not persist"})))
    assert denied.status_code == 403
    assert read_only_database.connection.execute("SELECT COUNT(*) FROM cf_conversations").fetchone()[0] == 0
    read_only_database.connection.close()

    database, env = environment(scopes=READ_SCOPES + ["conversations:write"])
    invalid = run(
        create_developer_conversation_from_segments(
            FakeRequest(env, body={"transcript_segments": [{"text": "Bad", "start": 2, "end": 1}]})
        )
    )
    assert invalid.status_code == 422
    assert database.connection.execute("SELECT COUNT(*) FROM cf_conversations").fetchone()[0] == 0
    database.connection.close()
