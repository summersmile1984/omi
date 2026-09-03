import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from goal_ai_routes import (  # noqa: E402
    extract_goal_progress,
    get_current_goal_advice,
    get_goal_advice,
    suggest_goal,
)

SECRET = "goal-ai-secret"
UID = "goal-ai-user"


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


class FakeAi:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        if "text" in payload and "messages" not in payload:
            return {"data": [[0.01] * 1024 for _ in payload["text"]]}
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return {"response": response}


class FakeVectorIndex:
    def __init__(self):
        self.matches = []
        self.calls = []

    async def query(self, vector, options):
        self.calls.append((vector, options))
        return {"count": len(self.matches), "matches": list(self.matches)}


class FakeRequest:
    def __init__(self, env, *, body=None, authenticated=True, headers=None):
        self.scope = {"env": env}
        self.headers = (signed_headers() if authenticated else {}) | (headers or {})
        self.query_params = {}
        self._body = b"" if body is None else json.dumps(body).encode()

    async def body(self):
        return self._body


def signed_headers(*, account_created_at=None):
    context = {"uid": UID, "authority": "better-auth", "requestId": "goal-ai-test"}
    if account_created_at is not None:
        context["accountCreatedAt"] = account_created_at
    raw = json.dumps(context, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def environment(responses=None):
    database = FakeDb()
    env = SimpleNamespace(
        APP_DB=database,
        AI=FakeAi(responses),
        CONVERSATION_VECTORS=FakeVectorIndex(),
        INTERNAL_ASSERTION_SECRET=SECRET,
        WORKERS_AI_SYNTHESIS_MODEL="test-goal-model",
        WORKERS_AI_VECTOR_MODEL="test-vector-model",
        FREE_CHAT_QUESTIONS_PER_MONTH="30",
        TRIAL_PAYWALL_ENABLED="false",
    )
    return database, env


def response_json(response):
    return json.loads(response.body)


def insert_memory(database, memory_id="memory-1", content="The user runs every Friday.", *, updated_at=None):
    now = updated_at or int(datetime.now(timezone.utc).timestamp())
    database.connection.execute(
        "INSERT INTO cf_memories "
        "(uid,id,content,category,visibility,tags_json,subject_attribution,object_entity_ids_json,qualifiers_json,"
        "uncertainty_reasons_json,reviewed,manually_added,memory_tier,valid_at,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            UID,
            memory_id,
            content,
            "interesting",
            "private",
            "[]",
            "user",
            "[]",
            "{}",
            "[]",
            0,
            0,
            "long_term",
            now,
            now,
            now,
        ),
    )
    database.connection.commit()


def insert_goal(database, goal_id="goal-run", *, current=2, target=10, focused=True):
    now = int(datetime.now(timezone.utc).timestamp())
    metric = {"type": "numeric", "current": current, "target": target, "min": 0, "max": target, "unit": "runs"}
    database.connection.execute(
        "INSERT INTO cf_goals "
        "(uid,id,title,desired_outcome,why_it_matters,success_criteria_json,status,focus_rank,metric_json,source,"
        "relationship_disposition,is_active,latest_progress_sequence,created_at,updated_at) "
        "VALUES (?,?,?,?,?,'[]',?,?,?,?, 'retain',1,0,?,?)",
        (
            UID,
            goal_id,
            "Run 10 times",
            "Run ten times this month",
            "Build a consistent routine",
            "focused" if focused else "background",
            0 if focused else None,
            json.dumps(metric),
            "user",
            now,
            now,
        ),
    )
    database.connection.commit()


def insert_advice_context(database):
    now = int(datetime.now(timezone.utc).timestamp())
    database.connection.execute(
        "INSERT INTO cf_conversations "
        "(uid,id,created_at,updated_at,started_at,finished_at,source,status,structured_json,transcript_segments_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            UID,
            "conversation-run",
            now,
            now,
            now,
            now + 60,
            "desktop",
            "completed",
            json.dumps({"title": "Training plan", "overview": "The user planned a Friday morning run."}),
            "[]",
        ),
    )
    database.connection.execute(
        "INSERT INTO cf_chat_messages (uid,id,app_id,created_at,message_json) VALUES (?,?,NULL,?,?)",
        (
            UID,
            "chat-run",
            now,
            json.dumps({"id": "chat-run", "sender": "human", "text": "I can run before work on Friday."}),
        ),
    )
    vector_id = "c" * 64
    database.connection.execute(
        "INSERT INTO cf_vector_projection_state "
        "(uid,projection_kind,source_id,sub_id,vector_id,source_version,model,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (UID, "conversation", "conversation-run", "", vector_id, now, "test-vector-model", now),
    )
    database.connection.commit()
    return vector_id


def test_goal_suggestion_is_authenticated_uses_default_without_memories_and_workers_ai_with_context():
    database, env = environment(
        [
            {
                "suggested_title": "Run three mornings weekly",
                "suggested_type": "numeric",
                "suggested_target": 3,
                "suggested_min": 0,
                "suggested_max": 3,
                "reasoning": "The user already runs on Fridays.",
            }
        ]
    )
    unauthorized = asyncio.run(suggest_goal(FakeRequest(env, authenticated=False)))
    assert unauthorized.status_code == 401
    assert asyncio.run(suggest_goal(FakeRequest(env))) == {
        "suggested_title": "Learn something new every day",
        "suggested_type": "scale",
        "suggested_target": 10.0,
        "suggested_min": 0.0,
        "suggested_max": 10.0,
        "reasoning": "Start tracking your daily learning progress!",
    }
    assert env.AI.calls == []

    insert_memory(database)
    suggestion = asyncio.run(suggest_goal(FakeRequest(env)))
    assert suggestion["suggested_title"] == "Run three mornings weekly"
    assert suggestion["suggested_target"] == 3.0
    assert "runs every Friday" in env.AI.calls[0][1]["messages"][1]["content"]


def test_goal_advice_uses_vector_recent_chat_and_memory_context_and_is_uid_scoped():
    database, env = environment([{"advice": "Schedule Friday's run before the first meeting."}])
    insert_goal(database)
    insert_memory(database)
    vector_id = insert_advice_context(database)
    env.CONVERSATION_VECTORS.matches = [{"id": vector_id, "score": 0.93}]

    advice = asyncio.run(get_goal_advice(FakeRequest(env), "goal-run"))
    assert advice == {"advice": "Schedule Friday's run before the first meeting."}
    assert len(env.CONVERSATION_VECTORS.calls) == 1
    prompt = env.AI.calls[-1][1]["messages"][1]["content"]
    assert "Friday morning run" in prompt
    assert "before work on Friday" in prompt
    assert "runs every Friday" in prompt

    missing = asyncio.run(get_goal_advice(FakeRequest(env), "missing"))
    assert missing.status_code == 404
    assert response_json(missing) == {"detail": "Goal not found"}


def test_current_goal_advice_handles_empty_goal_and_provider_failure_safely():
    database, env = environment([])
    assert asyncio.run(get_current_goal_advice(FakeRequest(env))) == {
        "advice": "Set a goal to get personalized advice!"
    }
    insert_goal(database)
    env.AI.responses = [RuntimeError("offline")]
    assert asyncio.run(get_current_goal_advice(FakeRequest(env))) == {
        "advice": "Focus on the next small step toward your goal."
    }


def test_goal_progress_extraction_updates_goal_event_and_history_in_one_batch():
    database, env = environment(
        [
            {
                "updates": [
                    {"goal_id": "goal-run", "found": True, "value": 5, "reasoning": "User said total is five."},
                    {"goal_id": "unknown", "found": True, "value": 99, "reasoning": "Ignore unknown goal."},
                ]
            }
        ]
    )
    insert_goal(database)
    result = asyncio.run(extract_goal_progress(FakeRequest(env, body={"text": "I have completed five runs in total."})))
    assert result == {
        "updated": True,
        "reason": None,
        "updates": [
            {
                "goal_id": "goal-run",
                "goal_title": "Run 10 times",
                "previous_value": 2.0,
                "new_value": 5.0,
                "reasoning": "User said total is five.",
            }
        ],
    }
    goal = database.connection.execute(
        "SELECT metric_json, latest_progress_sequence FROM cf_goals WHERE uid = ? AND id = ?",
        (UID, "goal-run"),
    ).fetchone()
    assert json.loads(goal["metric_json"])["current"] == 5.0
    assert goal["latest_progress_sequence"] == 1
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM cf_goal_progress_events WHERE uid = ? AND goal_id = ?", (UID, "goal-run")
        ).fetchone()[0]
        == 1
    )
    assert (
        database.connection.execute(
            "SELECT value FROM cf_goal_progress_history WHERE uid = ? AND goal_id = ?", (UID, "goal-run")
        ).fetchone()[0]
        == 5.0
    )
    assert "absolute progress totals" in env.AI.calls[0][1]["messages"][0]["content"]


def test_goal_progress_validation_no_active_goal_and_free_quota_gate():
    database, env = environment([])
    invalid = asyncio.run(extract_goal_progress(FakeRequest(env, body={"text": ""})))
    assert invalid.status_code == 422
    assert asyncio.run(extract_goal_progress(FakeRequest(env, body={"text": "short message"}))) == {
        "updated": False,
        "reason": "No active goal",
        "updates": [],
    }

    now = int(datetime.now(timezone.utc).timestamp())
    database.connection.executemany(
        "INSERT INTO cf_chat_quota_events "
        "(uid,idempotency_key,source,message_id,chat_session_id,platform,occurred_at) VALUES (?,?,?,?,?,?,?)",
        [(UID, f"quota-{index}", "v2_messages", f"m-{index}", "session", "desktop", now) for index in range(30)],
    )
    database.connection.commit()
    insert_goal(database)
    blocked = asyncio.run(
        extract_goal_progress(
            FakeRequest(env, body={"text": "I completed five runs total."}, headers={"x-app-platform": "desktop"})
        )
    )
    assert blocked.status_code == 402
    assert response_json(blocked)["detail"]["error"] == "quota_exceeded"
    assert env.AI.calls == []
