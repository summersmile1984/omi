import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from internal_auth import verify_request_context  # noqa: E402
from mcp_routes import (  # noqa: E402
    SUPPORTED_SCOPES,
    _memory_id,
    complete_action_item,
    create_action_item,
    create_memory,
    delete_action_item,
    delete_memory,
    edit_memory,
    get_action_items,
    get_chat,
    get_conversation,
    get_conversations,
    get_daily_summaries,
    get_goals,
    get_memories,
    get_people,
    get_profile,
    get_screen_activity,
    update_action_item,
)

RAW_SECRET = "0123456789abcdef0123456789abcdef"
AUTHORIZATION = f"Bearer omi_mcp_{RAW_SECRET}"
INTERNAL_SECRET = "mcp-internal-test-secret"


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
    def __init__(self, env, *, body=None, query=None, authorization=AUTHORIZATION):
        self.scope = {"env": env}
        self.headers = {"x-request-id": "mcp-route-test"}
        if authorization is not None:
            self.headers["authorization"] = authorization
        self.query_params = Query(query or {})
        self._body = b"" if body is None else json.dumps(body).encode()

    async def body(self):
        return self._body


class FakeAi:
    def __init__(self, category="interesting"):
        self.category = category
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        return {"response": {"category": self.category}}


class FakeAuthResponse:
    status = 200

    async def json(self):
        return {"uid": "mcp-user", "name": "MCP User", "email": "mcp@example.test"}


class FakeAuth:
    def __init__(self):
        self.calls = []

    async def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeAuthResponse()


def environment(*, scopes=None, state="new", key_prefix=None):
    db = FakeDb()
    auth = FakeAuth()
    ai = FakeAi()
    env = SimpleNamespace(
        APP_DB=db,
        AUTH=auth,
        AI=ai,
        INTERNAL_ASSERTION_SECRET=INTERNAL_SECRET,
        WORKERS_AI_INTEGRATION_MODEL="test-model",
    )
    digest = hashlib.sha256(RAW_SECRET.encode()).hexdigest()
    prefix = key_prefix or f"omi_mcp_{RAW_SECRET[:4]}...{RAW_SECRET[-4:]}"
    db.connection.execute(
        "INSERT INTO cf_mcp_api_keys "
        "(uid, key_id, name, key_hash, key_prefix, app_id, scopes_json, created_at) "
        "VALUES ('mcp-user', 'key-1', 'Test key', ?, ?, 'mcp-api', ?, 1)",
        (digest, prefix, json.dumps(sorted(scopes or SUPPORTED_SCOPES))),
    )
    db.connection.execute(
        "INSERT INTO cf_account_cutover "
        "(uid, state, checkpoint_phase, destination_backend_bound, updated_at) "
        "VALUES ('mcp-user', ?, 'completed', 1, 1)",
        (state,),
    )
    db.connection.commit()
    return db, env


def response_body(response):
    return json.loads(response.body)


def run(awaitable):
    return asyncio.run(awaitable)


def test_mcp_key_auth_is_exact_scoped_and_fenced_to_active_cloudflare_accounts():
    db, env = environment(key_prefix="omi_mcp_legacy")
    assert run(get_memories(FakeRequest(env))) == []

    missing = run(get_memories(FakeRequest(env, authorization=None)))
    assert missing.status_code == 401
    malformed = run(get_memories(FakeRequest(env, authorization=f"Bearer omi_mcp_{'A' * 32}")))
    assert malformed.status_code == 403

    db.connection.execute("UPDATE cf_mcp_api_keys SET scopes_json = '[\"memories.read\"]'")
    db.connection.commit()
    denied = run(get_action_items(FakeRequest(env)))
    assert denied.status_code == 403
    assert response_body(denied)["detail"].endswith("action_items.read")

    db.connection.execute("UPDATE cf_mcp_api_keys SET scopes_json = '[\"unknown.scope\"]'")
    db.connection.commit()
    corrupt = run(get_memories(FakeRequest(env)))
    assert corrupt.status_code == 503

    _, legacy_env = environment(state="legacy")
    inactive = run(get_memories(FakeRequest(legacy_env)))
    assert inactive.status_code == 409

    deletion_db, deletion_env = environment()
    deletion_db.connection.execute(
        "INSERT INTO cf_account_deletion_intents "
        "(uid, job_id, status, phase, next_attempt_at, created_at, updated_at) "
        "VALUES ('mcp-user', 'delete-1', 'pending', 'quiescing', 1, 1, 1)"
    )
    deletion_db.connection.commit()
    deleting = run(get_memories(FakeRequest(deletion_env)))
    assert deleting.status_code == 409


def test_mcp_profile_uses_a_request_bound_auth_service_assertion():
    db, env = environment()
    db.connection.execute(
        "INSERT INTO cf_user_ai_profiles "
        "(uid, profile_text, generated_at, data_sources_used, created_at, updated_at) "
        "VALUES ('mcp-user', 'Likes tea', '2026-08-30T00:00:00Z', 4, 1, 1)"
    )
    db.connection.commit()

    profile = run(get_profile(FakeRequest(env)))
    assert profile == {
        "name": "MCP User",
        "email": "mcp@example.test",
        "phone_number": None,
        "profile_text": "Likes tea",
        "generated_at": "2026-08-30T00:00:00Z",
        "data_sources_used": 4,
    }
    url, kwargs = env.AUTH.calls[0]
    assert url == "https://auth.internal/internal/profile"
    context = verify_request_context(
        kwargs["headers"]["x-omi-auth-context"],
        kwargs["headers"]["x-omi-internal-signature"],
        INTERNAL_SECRET,
        audience="auth",
        method="GET",
        path="/internal/profile",
        now=int(time.time()),
    )
    assert context is not None
    assert context["uid"] == "mcp-user"
    assert context["authority"] == "internal"


def test_mcp_memory_create_list_edit_delete_is_uid_scoped_and_uses_workers_ai():
    db, env = environment()
    created = run(
        create_memory(
            FakeRequest(
                env,
                body={
                    "content": "The user prefers green tea.",
                    "category": "caller-value-is-ignored",
                    "tags": ["profile"],
                },
            )
        )
    )
    assert created["category"] == "interesting"
    memory_id = _memory_id("The user prefers green tea.")
    stored = db.connection.execute(
        "SELECT category, reviewed, user_review, manually_added, memory_tier FROM cf_memories WHERE id = ?",
        (memory_id,),
    ).fetchone()
    assert dict(stored) == {
        "category": "interesting",
        "reviewed": 1,
        "user_review": 1,
        "manually_added": 1,
        "memory_tier": "long_term",
    }
    assert len(env.AI.calls) == 1

    db.connection.execute(
        "INSERT INTO cf_memories "
        "(uid, id, content, category, memory_tier, valid_at, created_at, updated_at) "
        "VALUES ('other-user', 'other-memory', 'private', 'system', 'long_term', 1, 1, 1)"
    )
    db.connection.commit()
    listed = run(get_memories(FakeRequest(env)))
    assert [item["id"] for item in listed] == [memory_id]

    assert run(edit_memory(FakeRequest(env, query={"value": "Updated memory"}), memory_id)) == {"status": "ok"}
    assert run(delete_memory(FakeRequest(env), memory_id)) == {"status": "ok"}
    assert run(get_memories(FakeRequest(env))) == []
    missing = run(delete_memory(FakeRequest(env), "other-memory"))
    assert missing.status_code == 404


def test_mcp_conversations_skip_malformed_rows_and_protect_locked_detail():
    db, env = environment()
    valid_structured = json.dumps({"title": "Tea", "overview": "Planning tea", "category": "other"})
    transcript = json.dumps(
        [{"id": "segment-1", "text": "Hello", "speaker_id": 0, "is_user": True, "start": 0, "end": 1}]
    )
    for conversation_id, structured, locked, uid in (
        ("valid", valid_structured, 0, "mcp-user"),
        ("locked", valid_structured, 1, "mcp-user"),
        ("malformed", "{}", 0, "mcp-user"),
        ("other", valid_structured, 0, "other-user"),
    ):
        db.connection.execute(
            "INSERT INTO cf_conversations "
            "(uid, id, created_at, started_at, finished_at, status, discarded, is_locked, structured_json, "
            "transcript_segments_json, apps_results_json) VALUES (?, ?, 10, 10, 11, 'completed', 0, ?, ?, ?, '[]')",
            (uid, conversation_id, locked, structured, transcript),
        )
    db.connection.commit()

    listed = run(get_conversations(FakeRequest(env)))
    assert {item["id"] for item in listed} == {"valid", "locked"}
    assert next(item for item in listed if item["id"] == "locked")["apps_results"] == []
    detail = run(get_conversation(FakeRequest(env), "valid"))
    assert detail["transcript_segments"][0]["speaker_name"] == "User"
    locked = run(get_conversation(FakeRequest(env), "locked"))
    assert locked.status_code == 402
    malformed = run(get_conversation(FakeRequest(env), "malformed"))
    assert malformed.status_code == 404
    other = run(get_conversation(FakeRequest(env), "other"))
    assert other.status_code == 404


def test_mcp_action_item_crud_is_idempotent_and_locked_writes_fail_closed():
    db, env = environment()
    request = FakeRequest(env, body={"description": "Ship the Cloudflare adapter"})
    first = run(create_action_item(request))
    second = run(create_action_item(FakeRequest(env, body={"description": "Ship the Cloudflare adapter"})))
    assert first["id"] == second["id"]

    updated = run(
        update_action_item(
            FakeRequest(env, body={"description": "Ship staging", "due_at": "2026-09-01T00:00:00Z"}),
            first["id"],
        )
    )
    assert updated["description"] == "Ship staging"
    completed = run(complete_action_item(FakeRequest(env), first["id"]))
    assert completed["completed"] is True

    db.connection.execute(
        "INSERT INTO cf_action_items "
        "(uid, id, description, status, completed, is_locked, created_at, updated_at) "
        "VALUES ('mcp-user', 'locked-item', 'Locked item', 'active', 0, 1, 1, 1)"
    )
    db.connection.commit()
    locked = run(delete_action_item(FakeRequest(env), "locked-item"))
    assert locked.status_code == 402
    assert run(delete_action_item(FakeRequest(env), first["id"])) == {"status": "ok"}
    assert run(get_action_items(FakeRequest(env))) == [
        {
            "id": "locked-item",
            "description": "Locked item",
            "completed": False,
            "created_at": "1970-01-01T00:00:01+00:00",
            "due_at": None,
            "completed_at": None,
            "conversation_id": None,
        }
    ]


def test_mcp_read_projections_cover_goals_chat_people_screen_and_daily_summaries():
    db, env = environment()
    db.connection.execute(
        "INSERT INTO cf_goals "
        "(uid, id, title, desired_outcome, status, source, created_at, updated_at) "
        "VALUES ('mcp-user', 'goal-1', 'Launch', 'Ship staging', 'focused', 'user', 1, 1)"
    )
    db.connection.execute(
        "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) "
        "VALUES ('mcp-user', 'message-1', NULL, 2, ?) ",
        (json.dumps({"text": "Hello", "sender": "user", "type": "text"}),),
    )
    db.connection.execute(
        "INSERT INTO cf_people "
        "(uid, id, name, speech_sample_transcripts_json, created_at, updated_at) "
        "VALUES ('mcp-user', 'person-1', 'Ada', ?, 1, 1)",
        (json.dumps(["one", "two", "three", "four", "five", "six"]),),
    )
    db.connection.execute(
        "INSERT INTO cf_screen_activity (uid, id, timestamp, app_name, window_title, ocr_text) "
        "VALUES ('mcp-user', 'screen-1', '2026-08-30 10:00:00.000', 'Terminal', 'Deploy', 'wrangler deploy')"
    )
    db.connection.execute(
        "INSERT INTO cf_daily_summaries "
        "(uid, id, date, overview, created_at, updated_at) "
        "VALUES ('mcp-user', 'summary-1', '2026-08-30', 'Cloudflare work', 1, 1)"
    )
    db.connection.commit()

    assert run(get_goals(FakeRequest(env)))[0]["id"] == "goal-1"
    assert run(get_chat(FakeRequest(env)))[0]["text"] == "Hello"
    assert run(get_people(FakeRequest(env)))[0]["speech_sample_transcripts"] == [
        "one",
        "two",
        "three",
        "four",
        "five",
    ]
    screen = run(get_screen_activity(FakeRequest(env, query={"summary": "true"})))
    assert screen["apps"]["Terminal"]["window_titles"] == ["Deploy"]
    assert run(get_daily_summaries(FakeRequest(env)))[0]["id"] == "summary-1"
