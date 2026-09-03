import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from persona_routes import get_or_create_user_persona, get_persona_initial_message  # noqa: E402


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
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0035_app_catalog.sql").read_text())
        self.connection.executescript((migration_dir / "0045_app_reviews.sql").read_text())
        self.connection.executescript((migration_dir / "0032_conversations.sql").read_text())
        self.connection.executescript((migration_dir / "0037_memories.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeAi:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        return self.response


class FakeAuthResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def json(self):
        return self.payload


class FakeAuth:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeAuthResponse(self.payload)


class FakeRequest:
    def __init__(self, env, headers=None):
        self.scope = {"env": env}
        self.headers = headers or {}


def signed_headers(secret="persona-secret", uid="persona-user", display_name=None):
    payload = {"uid": uid, "authority": "better-auth"}
    if display_name:
        payload["displayName"] = display_name
    raw = json.dumps(payload, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(ai=None, secret="persona-secret", auth=None):
    return type(
        "Env",
        (),
        {
            "APP_DB": FakeDb(),
            "AI": ai,
            "AUTH": auth,
            "INTERNAL_ASSERTION_SECRET": secret,
            "WORKERS_AI_INTEGRATION_MODEL": "test-persona-model",
        },
    )()


def seed_persona(env, *, username="alice", disabled=0, capabilities=None, prompt="You are Alice."):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_app_catalog (id, approved, disabled, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
        (
            "persona-alice",
            1,
            disabled,
            json.dumps(
                {
                    "id": "persona-alice",
                    "username": username,
                    "name": "Alice",
                    "capabilities": capabilities or ["persona"],
                    "persona_prompt": prompt,
                }
            ),
            1,
        ),
    )
    env.APP_DB.connection.commit()


def test_persona_initial_message_requires_auth_and_returns_empty_for_missing_or_invalid_persona():
    env = make_env()
    assert asyncio.run(get_persona_initial_message(FakeRequest(env), username="alice")).status_code == 401
    headers = signed_headers()
    assert asyncio.run(get_persona_initial_message(FakeRequest(env, headers), username="alice")) == {"message": ""}

    seed_persona(env, capabilities=["chat"])
    result = asyncio.run(get_persona_initial_message(FakeRequest(env, headers), username="alice"))
    assert result == {"message": ""}


def test_persona_initial_message_uses_d1_prompt_and_workers_ai():
    ai = FakeAi({"response": '"Want to chat about something fun?"'})
    env = make_env(ai)
    seed_persona(env)
    result = asyncio.run(get_persona_initial_message(FakeRequest(env, signed_headers()), username="@alice"))
    assert result == {"message": "Want to chat about something fun?"}
    assert len(ai.calls) == 1
    model, payload = ai.calls[0]
    assert model == "test-persona-model"
    assert payload["messages"][0] == {"role": "system", "content": "You are Alice."}
    assert "5-8 word" in payload["messages"][1]["content"]
    assert payload["max_tokens"] == 64
    assert payload["temperature"] == 0.7


def test_persona_initial_message_fails_closed_when_model_is_unavailable_or_malformed():
    env = make_env()
    seed_persona(env)
    unavailable = asyncio.run(get_persona_initial_message(FakeRequest(env, signed_headers()), username="alice"))
    assert unavailable.status_code == 503

    malformed_ai = FakeAi({"response": {"unexpected": "shape"}})
    env = make_env(malformed_ai)
    seed_persona(env)
    malformed = asyncio.run(get_persona_initial_message(FakeRequest(env, signed_headers()), username="alice"))
    assert malformed.status_code == 503


def test_default_persona_is_d1_backed_and_idempotent():
    auth = FakeAuth({"uid": "persona-user", "name": "Alice Example", "email": "alice@example.com"})
    env = make_env(auth=auth)
    headers = signed_headers()

    created = asyncio.run(get_or_create_user_persona(FakeRequest(env, headers)))
    repeated = asyncio.run(get_or_create_user_persona(FakeRequest(env, headers)))

    assert created["id"].startswith("cf_persona_")
    assert created["name"] == "Alice Example"
    assert created["username"] == "aliceexample"
    assert created["description"] == "This is Alice Example's personal AI clone."
    assert created["capabilities"] == ["persona"]
    assert created["private"] is True
    assert created["approved"] is False
    assert repeated["id"] == created["id"]
    assert len(auth.calls) == 1


def test_default_persona_prompt_uses_unlocked_d1_context_only():
    auth = FakeAuth({"uid": "persona-user", "name": "Alice", "email": "alice@example.com"})
    env = make_env(auth=auth)
    connection = env.APP_DB.connection
    connection.execute(
        "INSERT INTO cf_memories "
        "(uid, id, content, memory_tier, valid_at, created_at, updated_at, is_locked) "
        "VALUES (?, ?, ?, 'long_term', 1, 1, 2, ?)",
        ("persona-user", "memory-open", "Likes morning walks", 0),
    )
    connection.execute(
        "INSERT INTO cf_memories "
        "(uid, id, content, memory_tier, valid_at, created_at, updated_at, is_locked) "
        "VALUES (?, ?, ?, 'long_term', 1, 1, 2, ?)",
        ("persona-user", "memory-locked", "Private locked fact", 1),
    )
    connection.execute(
        "INSERT INTO cf_conversations "
        "(uid, id, created_at, status, structured_json, transcript_segments_json, is_locked) "
        "VALUES (?, ?, 1, 'completed', ?, '[]', 0)",
        ("persona-user", "conversation-1", json.dumps({"title": "Morning planning", "overview": "Plan the day"})),
    )
    connection.commit()

    result = asyncio.run(get_or_create_user_persona(FakeRequest(env, signed_headers())))
    prompt = result["persona_prompt"]
    assert "Likes morning walks" in prompt
    assert "Private locked fact" not in prompt
    assert "Morning planning" in prompt
