import asyncio
from datetime import datetime, timezone
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
    get_mcp_principal,
    get_memories,
    get_people,
    get_profile,
    get_screen_activity,
    search_action_items,
    search_conversations,
    search_memories,
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
    def __init__(
        self,
        env,
        *,
        body=None,
        query=None,
        authorization=AUTHORIZATION,
        auth_context=None,
        internal_secret=None,
    ):
        self.scope = {"env": env}
        if auth_context is not None:
            self.scope["state"] = {"auth_context": auth_context}
        self.headers = {"x-request-id": "mcp-route-test"}
        if authorization is not None:
            self.headers["authorization"] = authorization
        if internal_secret is not None:
            self.headers["x-internal-assertion-secret"] = internal_secret
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
        if "text" in payload:
            return {"data": [[0.01] * 1024 for _ in payload["text"]]}
        return {"response": {"category": self.category}}


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
    queue = FakeQueue()
    env = SimpleNamespace(
        APP_DB=db,
        AUTH=auth,
        AI=ai,
        JOBS=queue,
        MEMORY_VECTORS=FakeVectorIndex(),
        ACTION_ITEM_VECTORS=FakeVectorIndex(),
        CONVERSATION_VECTORS=FakeVectorIndex(),
        TRANSCRIPT_CHUNK_VECTORS=FakeVectorIndex(),
        INTERNAL_ASSERTION_SECRET=INTERNAL_SECRET,
        WORKERS_AI_INTEGRATION_MODEL="test-model",
        WORKERS_AI_VECTOR_MODEL="test-vector-model",
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


def insert_vector_state(db, kind, source_id, vector_id, *, sub_id="000000", version=10):
    db.connection.execute(
        "INSERT INTO cf_vector_projection_state "
        "(uid, projection_kind, source_id, sub_id, vector_id, source_version, model, updated_at) "
        "VALUES ('mcp-user', ?, ?, ?, ?, ?, 'test-vector-model', ?)",
        (kind, source_id, sub_id, vector_id, version, version),
    )


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


def test_mcp_oauth_context_uses_the_same_scope_and_data_plane_fences_without_touching_api_keys():
    db, env = environment()
    context = {
        "uid": "mcp-user",
        "authority": "mcp-oauth",
        "scopes": ["memories.read"],
        "oauthClientId": "mcp-oauth-client",
    }
    request = FakeRequest(env, authorization=None, auth_context=context)
    assert run(get_memories(request)) == []
    key = db.connection.execute("SELECT last_used_at FROM cf_mcp_api_keys WHERE key_id = 'key-1'").fetchone()
    assert key["last_used_at"] is None

    principal = run(get_mcp_principal(FakeRequest(env, authorization=None, auth_context=context)))
    assert principal == {
        "uid": "mcp-user",
        "scopes": ["memories.read"],
        "auth_type": "oauth",
        "client_id": "mcp-oauth-client",
    }

    denied = run(get_action_items(FakeRequest(env, authorization=None, auth_context=context)))
    assert denied.status_code == 403
    assert response_body(denied)["detail"].endswith("action_items.read")

    corrupt = run(
        get_memories(
            FakeRequest(
                env,
                authorization=None,
                auth_context={**context, "scopes": ["memories.read", "unknown.scope"]},
            )
        )
    )
    assert corrupt.status_code == 503

    _, inactive_env = environment(state="legacy")
    inactive = run(get_memories(FakeRequest(inactive_env, authorization=None, auth_context=context)))
    assert inactive.status_code == 409


def test_mcp_internal_principal_requires_the_edge_secret_for_api_keys():
    _, env = environment()
    unsigned = run(get_mcp_principal(FakeRequest(env)))
    assert unsigned.status_code == 401

    principal = run(get_mcp_principal(FakeRequest(env, internal_secret=INTERNAL_SECRET)))
    assert principal == {
        "uid": "mcp-user",
        "scopes": sorted(SUPPORTED_SCOPES),
        "auth_type": "api_key",
        "client_id": None,
    }


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
    projection = db.connection.execute(
        "SELECT operation FROM cf_vector_projection_outbox WHERE uid = 'mcp-user' AND source_kind = 'memory'"
    ).fetchone()
    assert dict(projection) == {"operation": "delete"}
    assert [message["kind"] for message in env.JOBS.messages] == ["vector_project"] * 3
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


def test_mcp_memory_search_uses_vector_candidates_then_hydrates_active_d1_rows():
    db, env = environment()
    locked_id = "locked-memory"
    visible_id = "visible-memory"
    for memory_id, content, locked in (
        (locked_id, "Private launch phrase", 1),
        (visible_id, "The launch plan uses Cloudflare", 0),
    ):
        db.connection.execute(
            "INSERT INTO cf_memories "
            "(uid, id, content, category, reviewed, user_review, is_locked, memory_tier, valid_at, created_at, updated_at) "
            "VALUES ('mcp-user', ?, ?, 'system', 1, 1, ?, 'long_term', 10, 10, 10)",
            (memory_id, content, locked),
        )
    locked_vector = "a" * 64
    visible_vector = "b" * 64
    insert_vector_state(db, "memory", locked_id, locked_vector)
    insert_vector_state(db, "memory", visible_id, visible_vector)
    db.connection.commit()
    env.MEMORY_VECTORS.matches = [
        {"id": locked_vector, "score": 0.98},
        {"id": visible_vector, "score": 0.87},
    ]

    result = run(search_memories(FakeRequest(env, query={"query": "launch", "limit": "1"})))

    assert result == [
        {
            "id": visible_id,
            "content": "The launch plan uses Cloudflare",
            "category": "system",
            "relevance_score": 0.87,
        }
    ]
    assert env.MEMORY_VECTORS.calls[0][1]["topK"] == 3
    assert len(env.MEMORY_VECTORS.calls[0][1]["namespace"]) == 64


def test_mcp_action_item_search_preserves_vector_order_and_threshold():
    db, env = environment()
    for item_id, description in (("action-1", "Deploy staging"), ("action-2", "Buy tea")):
        db.connection.execute(
            "INSERT INTO cf_action_items "
            "(uid, id, description, status, completed, created_at, updated_at) "
            "VALUES ('mcp-user', ?, ?, 'active', 0, 10, 10)",
            (item_id, description),
        )
    first_vector = "c" * 64
    second_vector = "d" * 64
    insert_vector_state(db, "action_item", "action-1", first_vector)
    insert_vector_state(db, "action_item", "action-2", second_vector)
    db.connection.commit()
    env.ACTION_ITEM_VECTORS.matches = [
        {"id": first_vector, "score": 0.91},
        {"id": second_vector, "score": 0.29},
    ]

    result = run(search_action_items(FakeRequest(env, query={"query": "release", "limit": "10"})))

    assert [item["id"] for item in result] == ["action-1"]
    missing = run(search_action_items(FakeRequest(env, query={"query": ""})))
    assert missing.status_code == 422


def test_mcp_conversation_search_merges_transcript_first_filters_dates_and_attaches_snippets():
    db, env = environment()
    created_at = int(datetime(2026, 8, 30, 12, tzinfo=timezone.utc).timestamp())
    structured = json.dumps({"title": "Launch", "overview": "Staging review", "category": "work"})
    transcript = json.dumps(
        [
            {"id": "s1", "text": "We confirmed the launch window", "speaker_id": 0, "start": 1, "end": 2},
            {"id": "s2", "text": "It is tomorrow", "is_user": True, "speaker_id": 1, "start": 2, "end": 3},
        ]
    )
    for conversation_id in ("summary-hit", "transcript-hit"):
        db.connection.execute(
            "INSERT INTO cf_conversations "
            "(uid, id, created_at, updated_at, started_at, finished_at, status, discarded, structured_json, "
            "transcript_segments_json, apps_results_json) "
            "VALUES ('mcp-user', ?, ?, ?, ?, ?, 'completed', 0, ?, ?, '[]')",
            (conversation_id, created_at, created_at, created_at, created_at + 60, structured, transcript),
        )
    summary_vector = "e" * 64
    transcript_vector = "f" * 64
    insert_vector_state(db, "conversation", "summary-hit", summary_vector, version=created_at)
    insert_vector_state(
        db,
        "transcript_chunk",
        "transcript-hit",
        transcript_vector,
        sub_id="000001",
        version=created_at,
    )
    db.connection.commit()
    env.CONVERSATION_VECTORS.matches = [{"id": summary_vector, "score": 0.8}]
    env.TRANSCRIPT_CHUNK_VECTORS.matches = [{"id": transcript_vector, "score": 0.9}]

    result = run(
        search_conversations(
            FakeRequest(
                env,
                query={
                    "query": "launch window",
                    "limit": "10",
                    "start_date": "2026-08-30",
                    "end_date": "2026-08-30",
                },
            )
        )
    )

    assert [conversation["id"] for conversation in result] == ["transcript-hit", "summary-hit"]
    assert result[0]["match_snippets"][0]["segment_id"] == "s1"
    assert "launch window" in result[0]["match_snippets"][0]["text"]
    for index in (env.CONVERSATION_VECTORS, env.TRANSCRIPT_CHUNK_VECTORS):
        created_filter = index.calls[0][1]["filter"]["created_at"]
        assert set(created_filter) == {"$gte", "$lte"}
    invalid = run(search_conversations(FakeRequest(env, query={"query": "launch", "start_date": "nope"})))
    assert invalid.status_code == 400


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
    projection = db.connection.execute(
        "SELECT operation FROM cf_vector_projection_outbox " "WHERE uid = 'mcp-user' AND source_kind = 'action_item'"
    ).fetchone()
    assert dict(projection) == {"operation": "delete"}
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
