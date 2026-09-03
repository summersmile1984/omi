import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from followup_routes import get_followup_question  # noqa: E402


class FakeStatement:
    def __init__(self, connection, sql):
        self.connection = connection
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def first(self):
        row = self.connection.execute(self.sql, self.args).fetchone()
        return dict(row) if row is not None else None


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration = Path(__file__).parents[3] / "migrations/app/0032_conversations.sql"
        self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeAi:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeRequest:
    def __init__(self, env, headers=None):
        self.scope = {"env": env}
        self.headers = headers or {}


def signed_headers(secret="followup-secret", uid="followup-user"):
    raw = json.dumps({"uid": uid, "authority": "better-auth"}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(*, ai=None, secret="followup-secret", model="test-followup-model"):
    return type(
        "Env",
        (),
        {
            "APP_DB": FakeDb(),
            "AI": ai,
            "INTERNAL_ASSERTION_SECRET": secret,
            "WORKERS_AI_INTEGRATION_MODEL": model,
        },
    )()


def seed_conversation(
    env,
    *,
    uid="followup-user",
    conversation_id="conversation-1",
    status="completed",
    locked=0,
    segments=None,
    updated_at=10,
):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_conversations "
        "(uid, id, created_at, updated_at, status, is_locked, transcript_segments_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uid, conversation_id, 1, updated_at, status, locked, json.dumps(segments or [])),
    )
    env.APP_DB.connection.commit()


def test_followup_requires_auth_and_preserves_missing_locked_boundaries():
    env = make_env()
    assert asyncio.run(get_followup_question(FakeRequest(env), "conversation-1")).status_code == 401

    headers = signed_headers()
    missing = asyncio.run(get_followup_question(FakeRequest(env, headers), "conversation-1"))
    assert missing.status_code == 404
    assert json.loads(missing.body)["detail"] == "Conversation not found"

    no_progress = asyncio.run(get_followup_question(FakeRequest(env, headers), "0"))
    assert no_progress.status_code == 400
    assert json.loads(no_progress.body)["detail"] == "No memory in progres"

    seed_conversation(env, conversation_id="locked", locked=1)
    locked = asyncio.run(get_followup_question(FakeRequest(env, headers), "locked"))
    assert locked.status_code == 402
    assert json.loads(locked.body)["detail"] == "A paid plan is required to access this conversation."


def test_followup_short_transcript_returns_empty_without_workers_ai():
    ai = FakeAi({"response": "should not be called"})
    env = make_env(ai=ai)
    seed_conversation(
        env,
        segments=[{"text": "one two three", "is_user": True, "speaker": "SPEAKER_00"}],
    )

    result = asyncio.run(get_followup_question(FakeRequest(env, signed_headers()), "conversation-1"))
    assert result == {"result": ""}
    assert ai.calls == []


def test_followup_uses_bounded_transcript_speaker_labels_and_workers_ai():
    ai = FakeAi({"response": "What would you like to explore next?"})
    env = make_env(ai=ai)
    segments = [
        {"text": "User opening words", "is_user": True, "speaker": "SPEAKER_00"},
        {"text": "Guest has a thoughtful reply", "is_user": False, "speaker": "SPEAKER_02"},
    ]
    segments.extend({"text": f"word{i}", "is_user": True, "speaker": "SPEAKER_00"} for i in range(120))
    segments.append({"text": "The guest adds a final perspective", "is_user": False, "speaker": "SPEAKER_02"})
    seed_conversation(env, segments=segments)

    result = asyncio.run(get_followup_question(FakeRequest(env, signed_headers()), "conversation-1"))
    assert result == {"result": "What would you like to explore next?"}
    assert len(ai.calls) == 1
    model, payload = ai.calls[0]
    assert model == "test-followup-model"
    prompt = payload["messages"][0]["content"]
    assert "User opening words" not in prompt
    assert "Guest has a thoughtful reply" not in prompt
    assert "word20" not in prompt
    assert "word74" in prompt
    assert "word119" in prompt
    assert "Speaker 2:" in prompt
    assert payload["max_tokens"] == 128
    assert payload["temperature"] == 0


def test_followup_fails_closed_when_workers_ai_is_unavailable_or_malformed():
    env = make_env()
    seed_conversation(
        env,
        segments=[
            {
                "text": "This transcript has enough words for generation now and should produce a question",
                "is_user": True,
            }
        ],
    )
    unavailable = asyncio.run(get_followup_question(FakeRequest(env, signed_headers()), "conversation-1"))
    assert unavailable.status_code == 503

    malformed_ai = FakeAi({"response": {"unexpected": "shape"}})
    env = make_env(ai=malformed_ai)
    seed_conversation(
        env,
        segments=[
            {
                "text": "This transcript has enough words for generation now and should produce a question",
                "is_user": True,
            }
        ],
    )
    malformed = asyncio.run(get_followup_question(FakeRequest(env, signed_headers()), "conversation-1"))
    assert malformed.status_code == 503
