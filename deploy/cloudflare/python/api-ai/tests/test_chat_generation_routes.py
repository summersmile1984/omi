import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import chat_generation_routes  # noqa: E402
from chat_generation_routes import (  # noqa: E402
    chat_messages,
    cloudflare_chat_completions,
    create_initial_message,
    create_session_initial_message,
    generate_reply,
    generate_session_title,
)
from chat_quota import reserve_chat_question  # noqa: E402


class FakeStatement:
    def __init__(self, connection, sql, *, fail_quota_run=False):
        self.connection = connection
        self.sql = sql
        self.args = ()
        self.fail_quota_run = fail_quota_run

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
        if self.fail_quota_run and "INSERT OR IGNORE INTO cf_chat_quota_events" in self.sql:
            raise RuntimeError("quota unavailable")
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class FakeDb:
    def __init__(self, *, fail_batch=False, fail_quota_run=False):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript(
            "CREATE TABLE cf_account_deletion_intents (uid TEXT PRIMARY KEY);"
            "CREATE TABLE cf_account_deletion_tombstones (uid TEXT PRIMARY KEY);"
        )
        for name in (
            "0010_user_assistant_profiles.sql",
            "0032_conversations.sql",
            "0035_app_catalog.sql",
            "0037_memories.sql",
            "0042_chat_messages.sql",
            "0046_account_usage.sql",
            "0054_chat_sessions.sql",
            "0055_chat_quota_accounting.sql",
            "0056_llm_usage_daily.sql",
        ):
            self.connection.executescript((migration_dir / name).read_text())
        self.connection.executescript(
            "ALTER TABLE cf_app_catalog ADD COLUMN owner_uid TEXT;"
            "CREATE TABLE cf_app_testers (uid TEXT PRIMARY KEY, added_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);"
        )
        self.fail_batch = fail_batch
        self.fail_quota_run = fail_quota_run

    def prepare(self, sql):
        return FakeStatement(self.connection, sql, fail_quota_run=self.fail_quota_run)

    async def batch(self, statements):
        if self.fail_batch:
            raise RuntimeError("unavailable")
        try:
            self.connection.execute("BEGIN")
            for statement in statements:
                self.connection.execute(statement.sql, statement.args)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return []


class FakeAi:
    def __init__(self, result=None, error=None):
        self.result = (
            result
            if result is not None
            else {
                "response": "Hello\nthere",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            }
        )
        self.error = error
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        if self.error:
            raise self.error
        return self.result


class FakeRequest:
    def __init__(self, env, headers=None, body=None, query=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.body = body if body is not None else {"text": "Current question", "file_ids": [], "context": None}
        self.query_params = query or {}

    async def json(self):
        return self.body


def signed_headers(
    secret: str,
    uid: str = "chat-user",
    account_created_at=None,
    byok_active: bool | None = None,
):
    context = {"uid": uid}
    if account_created_at is not None:
        context["accountCreatedAt"] = account_created_at
    if byok_active is not None:
        context["byokActive"] = byok_active
    raw = json.dumps(context, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


async def response_body(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.encode() if isinstance(chunk, str) else bytes(chunk))
    return b"".join(chunks)


def stored(uid: str, message_id: str, created_at: int, sender: str, text: str, session_id: str = "session-1"):
    return (
        uid,
        message_id,
        created_at,
        json.dumps(
            {
                "id": message_id,
                "sender": sender,
                "text": text,
                "chat_session_id": session_id,
                "session_id": session_id,
                "reported": False,
            }
        ),
    )


def test_default_text_chat_uses_workers_ai_persists_one_exchange_and_emits_legacy_sse():
    secret = "chat-secret"
    db = FakeDb()
    db.connection.executemany(
        "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) VALUES (?, ?, NULL, ?, ?)",
        [
            stored("chat-user", "m1", 1, "human", "Earlier question"),
            stored("chat-user", "m2", 2, "ai", "Earlier answer"),
            stored("other-user", "m3", 3, "human", "Private other-user text"),
        ],
    )
    db.connection.execute(
        "INSERT INTO cf_chat_sessions "
        "(uid, id, title, preview, created_at, updated_at, app_id, message_count, starred) "
        "VALUES ('chat-user', 'session-1', 'New Chat', NULL, 1, 2, NULL, 2, 0)"
    )
    db.connection.commit()
    ai = FakeAi()
    env = type(
        "Env",
        (),
        {
            "APP_DB": db,
            "AI": ai,
            "INTERNAL_ASSERTION_SECRET": secret,
            "WORKERS_AI_CHAT_MODEL": "@cf/test/chat",
        },
    )()

    response = asyncio.run(chat_messages(FakeRequest(env, signed_headers(secret))))
    body = asyncio.run(response_body(response)).decode()

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    assert body.startswith("data: Hello__CRLF__there\n\n")
    done = next(line.removeprefix("done: ") for line in body.splitlines() if line.startswith("done: "))
    final_message = json.loads(base64.b64decode(done))
    assert final_message["sender"] == "ai"
    assert final_message["text"] == "Hello\nthere"
    assert final_message["ask_for_nps"] is False
    assert final_message["files"] == []
    assert final_message["memories"] == []

    assert ai.calls[0][0] == "@cf/test/chat"
    assert ai.calls[0][1]["messages"] == [
        {
            "role": "system",
            "content": (
                "You are Omi, a concise and helpful personal assistant. Answer in the language used by the user. "
                "Do not claim access to memories, files, apps, tools, or live information that was not supplied "
                "in this chat."
            ),
        },
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": "Current question"},
    ]
    rows = db.connection.execute(
        "SELECT message_json FROM cf_chat_messages WHERE uid = 'chat-user' ORDER BY created_at, id"
    ).fetchall()
    persisted = [json.loads(row[0]) for row in rows]
    assert [message["sender"] for message in persisted[-2:]] == ["human", "ai"]
    assert [message["text"] for message in persisted[-2:]] == ["Current question", "Hello\nthere"]
    assert len(rows) == 4
    session = db.connection.execute(
        "SELECT message_count, preview FROM cf_chat_sessions WHERE uid = 'chat-user' AND id = 'session-1'"
    ).fetchone()
    assert dict(session) == {"message_count": 4, "preview": "Hello\nthere"}
    quota = db.connection.execute(
        "SELECT source, message_id, chat_session_id, platform, cost_usd, prompt_tokens, completion_tokens, model, "
        "settled_at IS NOT NULL AS settled FROM cf_chat_quota_events WHERE uid = 'chat-user'"
    ).fetchone()
    assert dict(quota) == {
        "source": "v2_messages",
        "message_id": persisted[-2]["id"],
        "chat_session_id": "session-1",
        "platform": None,
        "cost_usd": 0.0000118,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "model": "@cf/test/chat",
        "settled": 1,
    }


def test_session_initial_message_uses_d1_context_and_persists_one_ai_turn():
    secret = "chat-secret"
    db = FakeDb()
    now = int(__import__("time").time())
    db.connection.execute(
        "INSERT INTO cf_chat_sessions "
        "(uid, id, title, preview, created_at, updated_at, app_id, message_count, starred) "
        "VALUES ('chat-user', 'session-1', 'New Chat', NULL, ?, ?, NULL, 1, 0)",
        (now, now),
    )
    db.connection.execute(
        "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) VALUES (?, ?, NULL, ?, ?)",
        stored("chat-user", "prior", 1, "human", "We were planning Friday's deployment."),
    )
    db.connection.execute(
        "INSERT INTO cf_memories "
        "(uid,id,content,category,visibility,tags_json,subject_attribution,object_entity_ids_json,qualifiers_json,"
        "uncertainty_reasons_json,memory_tier,valid_at,created_at,updated_at) "
        "VALUES ('chat-user','memory-1','The user deploys on Fridays.','manual','private','[]','user','[]','{}','[]',"
        "'long_term',?,?,?)",
        (now, now, now),
    )
    db.connection.execute(
        "INSERT INTO cf_user_ai_profiles (uid,profile_text,created_at,updated_at) "
        "VALUES ('chat-user','- The user maintains Cloudflare infrastructure.',?,?)",
        (now, now),
    )
    db.connection.commit()
    ai = FakeAi(result={"response": "Ready for Friday's next deployment check?"})
    env = type(
        "Env",
        (),
        {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret, "WORKERS_AI_CHAT_MODEL": "@cf/test/chat"},
    )()

    result = asyncio.run(
        create_session_initial_message(FakeRequest(env, signed_headers(secret), body={"session_id": "session-1"}))
    )

    assert result["message"] == "Ready for Friday's next deployment check?"
    assert isinstance(result["message_id"], str)
    prompt = ai.calls[0][1]["messages"]
    assert "maintains Cloudflare infrastructure" in prompt[1]["content"]
    assert "deploys on Fridays" in prompt[1]["content"]
    assert prompt[-2] == {"role": "user", "content": "We were planning Friday's deployment."}
    persisted = db.connection.execute(
        "SELECT message_json FROM cf_chat_messages WHERE uid = 'chat-user' AND id = ?", (result["message_id"],)
    ).fetchone()
    message = json.loads(persisted[0])
    assert message["sender"] == "ai"
    assert message["chat_session_id"] == "session-1"
    session = db.connection.execute(
        "SELECT message_count, preview FROM cf_chat_sessions WHERE uid = 'chat-user' AND id = 'session-1'"
    ).fetchone()
    assert dict(session) == {"message_count": 2, "preview": "Ready for Friday's next deployment check?"}


def test_initial_message_alias_creates_app_session_and_honors_owned_persona_prompt():
    secret = "chat-secret"
    db = FakeDb()
    now = int(__import__("time").time())
    db.connection.execute(
        "INSERT INTO cf_app_catalog "
        "(id,approved,status,disabled,is_popular,installs,rating_count,data_json,updated_at,owner_uid) "
        "VALUES ('persona-1',0,'pending',0,0,0,0,?,?,?)",
        (
            json.dumps(
                {
                    "id": "persona-1",
                    "name": "Friday Coach",
                    "private": True,
                    "capabilities": ["persona"],
                    "persona_prompt": "Challenge the user to ship reliable infrastructure.",
                }
            ),
            now,
            "chat-user",
        ),
    )
    db.connection.commit()
    ai = FakeAi(result={"response": "ready to make friday boring?"})
    env = type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret})()

    result = asyncio.run(
        create_initial_message(FakeRequest(env, signed_headers(secret), query={"app_id": "persona-1"}))
    )

    assert result["text"] == "ready to make friday boring?"
    assert result["app_id"] == "persona-1"
    assert result["plugin_id"] == "persona-1"
    assert result["chat_session_id"] == result["session_id"]
    assert "Challenge the user" in ai.calls[0][1]["messages"][0]["content"]
    assert "provocative question" in ai.calls[0][1]["messages"][-1]["content"]
    session = db.connection.execute(
        "SELECT app_id, message_count FROM cf_chat_sessions WHERE uid = 'chat-user'"
    ).fetchone()
    assert dict(session) == {"app_id": "persona-1", "message_count": 1}


def test_initial_message_validation_missing_session_and_provider_failure_do_not_write():
    secret = "chat-secret"
    db = FakeDb()
    ai = FakeAi(error=RuntimeError("provider details"))
    env = type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)

    invalid = asyncio.run(create_session_initial_message(FakeRequest(env, headers, body={"session_id": ""})))
    missing = asyncio.run(create_session_initial_message(FakeRequest(env, headers, body={"session_id": "missing"})))
    provider = asyncio.run(create_initial_message(FakeRequest(env, headers)))

    assert invalid.status_code == 422
    assert missing.status_code == 404
    assert json.loads(missing.body) == {"detail": "Chat session not found"}
    assert provider.status_code == 502
    assert b"provider details" not in provider.body
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_messages").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_sessions").fetchone()[0] == 0


def test_initial_message_d1_batch_failure_returns_retry_without_partial_write():
    secret = "chat-secret"
    db = FakeDb(fail_batch=True)
    ai = FakeAi(result={"response": "Ready"})
    env = type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret})()

    response = asyncio.run(create_initial_message(FakeRequest(env, signed_headers(secret))))

    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "chat history unavailable"}
    assert len(ai.calls) == 1
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_messages").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_sessions").fetchone()[0] == 0


def test_generate_title_updates_only_the_callers_session_and_uses_empty_fallback():
    secret = "chat-secret"
    db = FakeDb()
    now = int(__import__("time").time())
    db.connection.executemany(
        "INSERT INTO cf_chat_sessions "
        "(uid,id,title,preview,created_at,updated_at,app_id,message_count,starred) "
        "VALUES (?, 'shared-session', 'New Chat', NULL, ?, ?, NULL, 0, 0)",
        [("chat-user", now, now), ("other-user", now, now)],
    )
    db.connection.commit()
    ai = FakeAi(result={"response": '"Cloudflare Migration Review"'})
    env = type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret})()

    result = asyncio.run(
        generate_session_title(
            FakeRequest(
                env,
                signed_headers(secret),
                body={
                    "session_id": "shared-session",
                    "messages": [
                        {"sender": "human", "text": "Review the Cloudflare migration"},
                        {"sender": "ai", "text": "Let's inspect the staging evidence"},
                    ],
                },
            )
        )
    )

    assert result == {"title": "Cloudflare Migration Review"}
    titles = db.connection.execute("SELECT uid, title FROM cf_chat_sessions ORDER BY uid").fetchall()
    assert [tuple(row) for row in titles] == [
        ("chat-user", "Cloudflare Migration Review"),
        ("other-user", "New Chat"),
    ]
    assert "Review the Cloudflare migration" in ai.calls[0][1]["messages"][1]["content"]

    ai.result = {"response": "  "}
    fallback = asyncio.run(
        generate_session_title(
            FakeRequest(
                env,
                signed_headers(secret),
                body={"session_id": "shared-session", "messages": [{"sender": "human", "text": "Hi"}]},
            )
        )
    )
    assert fallback == {"title": "New Chat"}


def test_chat_rejects_unsupported_modes_before_model_or_history_mutation():
    secret = "chat-secret"
    db = FakeDb()
    ai = FakeAi()
    env = type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)

    app_chat = asyncio.run(chat_messages(FakeRequest(env, headers, query={"app_id": "persona"})))
    attachments = asyncio.run(chat_messages(FakeRequest(env, headers, body={"text": "hello", "file_ids": ["f1"]})))
    page_context = asyncio.run(
        chat_messages(FakeRequest(env, headers, body={"text": "hello", "context": {"type": "task"}}))
    )

    assert app_chat.status_code == 409
    assert json.loads(app_chat.body)["reason"] == "app_chat_not_migrated"
    assert attachments.status_code == 409
    assert json.loads(attachments.body)["reason"] == "attachments_not_migrated"
    assert page_context.status_code == 409
    assert json.loads(page_context.body)["reason"] == "context_not_migrated"
    assert ai.calls == []
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_messages").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_sessions").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_quota_events").fetchone()[0] == 0


def test_free_quota_reservation_is_idempotent_and_hard_blocks_the_thirty_first_turn():
    secret = "chat-secret"
    db = FakeDb()
    ai = FakeAi(result={"response": "Allowed", "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    env = type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret})()
    now = int(__import__("time").time())
    db.connection.executemany(
        "INSERT INTO cf_chat_quota_events "
        "(uid, idempotency_key, source, occurred_at, cost_usd, settled_at) VALUES (?, ?, 'seed', ?, 0, ?)",
        [("chat-user", f"seed-{index}", now, now) for index in range(29)],
    )
    db.connection.commit()

    first = asyncio.run(chat_messages(FakeRequest(env, signed_headers(secret), body={"text": "slot 30"})))
    blocked = asyncio.run(chat_messages(FakeRequest(env, signed_headers(secret), body={"text": "slot 31"})))
    blocked_body = asyncio.run(response_body(blocked)).decode()

    assert first.status_code == 200
    assert blocked.status_code == 200
    assert len(ai.calls) == 1
    assert (
        db.connection.execute("SELECT COUNT(*) FROM cf_chat_quota_events WHERE uid = 'chat-user'").fetchone()[0] == 30
    )
    done = next(line.removeprefix("done: ") for line in blocked_body.splitlines() if line.startswith("done: "))
    message = json.loads(base64.b64decode(done))
    assert message["sender"] == "ai"
    assert "30 monthly chat question limit" in message["text"]
    assert "resets on" in message["text"]
    rows = db.connection.execute(
        "SELECT message_json FROM cf_chat_messages WHERE uid = 'chat-user' ORDER BY created_at, id"
    ).fetchall()
    assert [(json.loads(row[0])["sender"], json.loads(row[0])["text"]) for row in rows][-2:] == [
        ("human", "slot 31"),
        ("ai", message["text"]),
    ]

    reserved_once = asyncio.run(
        reserve_chat_question(
            env,
            uid="paid-user",
            idempotency_key="same-key",
            message_id="message-1",
            chat_session_id="session-1",
            platform="desktop",
            occurred_at=now,
        )
    )
    reserved_twice = asyncio.run(
        reserve_chat_question(
            env,
            uid="paid-user",
            idempotency_key="same-key",
            message_id="message-1",
            chat_session_id="session-1",
            platform="desktop",
            occurred_at=now,
        )
    )
    assert reserved_once is True
    assert reserved_twice is True
    assert (
        db.connection.execute(
            "SELECT COUNT(*) FROM cf_chat_quota_events WHERE uid = 'paid-user' AND idempotency_key = 'same-key'"
        ).fetchone()[0]
        == 1
    )


def test_chat_accounting_failure_returns_retry_sse_before_provider_or_message_write():
    secret = "chat-secret"
    db = FakeDb(fail_quota_run=True)
    ai = FakeAi()
    env = type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret})()

    response = asyncio.run(chat_messages(FakeRequest(env, signed_headers(secret))))
    body = asyncio.run(response_body(response)).decode()

    assert response.status_code == 200
    done = next(line.removeprefix("done: ") for line in body.splitlines() if line.startswith("done: "))
    message = json.loads(base64.b64decode(done))
    assert message["text"] == (
        "Usage accounting is temporarily unavailable. Please retry in a moment — your message was not saved."
    )
    assert ai.calls == []
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_messages").fetchone()[0] == 0


def test_expired_desktop_trial_rejects_unvalidated_byok_and_validated_byok_uses_openai(monkeypatch):
    secret = "chat-secret"
    db = FakeDb()
    ai = FakeAi()
    env = type(
        "Env",
        (),
        {
            "APP_DB": db,
            "AI": ai,
            "INTERNAL_ASSERTION_SECRET": secret,
            "TRIAL_PAYWALL_ENABLED": "true",
        },
    )()
    now = int(__import__("time").time())
    headers = {
        **signed_headers(secret, account_created_at=now - (4 * 24 * 60 * 60)),
        "x-app-platform": "desktop",
    }

    blocked = asyncio.run(chat_messages(FakeRequest(env, headers)))
    body = asyncio.run(response_body(blocked)).decode()

    assert blocked.status_code == 200
    assert ai.calls == []
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_quota_events").fetchone()[0] == 0
    done = next(line.removeprefix("done: ") for line in body.splitlines() if line.startswith("done: "))
    assert "30 monthly chat question limit" in json.loads(base64.b64decode(done))["text"]

    byok_headers = {
        **headers,
        "x-byok-openai": "key",
        "x-byok-anthropic": "key",
        "x-byok-gemini": "key",
        "x-byok-deepgram": "key",
    }
    unvalidated = asyncio.run(chat_messages(FakeRequest(env, byok_headers, body={"text": "Unvalidated BYOK"})))
    unvalidated_body = asyncio.run(response_body(unvalidated)).decode()
    assert unvalidated.status_code == 200
    assert ai.calls == []
    assert (
        "30 monthly chat question limit"
        in json.loads(
            base64.b64decode(
                next(line.removeprefix("done: ") for line in unvalidated_body.splitlines() if line.startswith("done: "))
            )
        )["text"]
    )

    provider_calls = []

    class FakeProviderResponse:
        status = 200

        async def json(self):
            return {"choices": [{"message": {"content": "BYOK answer"}}]}

    async def fake_provider_fetch(url, **options):
        provider_calls.append((url, options))
        return FakeProviderResponse()

    monkeypatch.setattr(chat_generation_routes, "worker_fetch", fake_provider_fetch)
    validated_headers = {
        **signed_headers(
            secret,
            account_created_at=now - (4 * 24 * 60 * 60),
            byok_active=True,
        ),
        "x-app-platform": "desktop",
        "x-byok-openai": "openai-user-key",
        "x-byok-anthropic": "anthropic-user-key",
        "x-byok-gemini": "gemini-user-key",
        "x-byok-deepgram": "deepgram-user-key",
    }
    allowed = asyncio.run(chat_messages(FakeRequest(env, validated_headers, body={"text": "BYOK request"})))
    allowed_body = asyncio.run(response_body(allowed)).decode()

    assert allowed.status_code == 200
    assert "BYOK answer" in allowed_body
    assert ai.calls == []
    assert len(provider_calls) == 1
    assert provider_calls[0][0] == "https://api.openai.com/v1/chat/completions"
    assert provider_calls[0][1]["headers"]["authorization"] == "Bearer openai-user-key"
    provider_body = json.loads(provider_calls[0][1]["body"])
    assert provider_body["model"] == "gpt-5.6-luna"
    assert provider_body["max_completion_tokens"] == 512
    assert "max_tokens" not in provider_body
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_quota_events").fetchone()[0] == 0


def test_paid_plan_overage_remains_allowed_past_its_question_allowance():
    secret = "chat-secret"
    db = FakeDb()
    ai = FakeAi(result={"response": "Overage served", "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    env = type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret})()
    now = int(__import__("time").time())
    db.connection.execute(
        "INSERT INTO cf_user_subscriptions (uid, plan, status, updated_at) "
        "VALUES ('chat-user', 'plus', 'active', ?)",
        (now,),
    )
    db.connection.executemany(
        "INSERT INTO cf_chat_quota_events "
        "(uid, idempotency_key, source, occurred_at, cost_usd, settled_at) VALUES (?, ?, 'seed', ?, 0, ?)",
        [("chat-user", f"paid-{index}", now, now) for index in range(200)],
    )
    db.connection.commit()

    response = asyncio.run(chat_messages(FakeRequest(env, signed_headers(secret))))

    assert response.status_code == 200
    assert len(ai.calls) == 1
    assert (
        db.connection.execute("SELECT COUNT(*) FROM cf_chat_quota_events WHERE uid = 'chat-user'").fetchone()[0] == 201
    )


def test_rapid_sequential_exchanges_keep_strict_history_order(monkeypatch):
    secret = "chat-secret"
    db = FakeDb()
    ai = FakeAi(
        result={
            "response": "Acknowledged",
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }
    )
    env = type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    order_keys = iter((2_000_000_000, 2_000_000_002))
    monkeypatch.setattr("chat_generation_routes._exchange_order_key", lambda: next(order_keys))

    first = asyncio.run(chat_messages(FakeRequest(env, headers, body={"text": "First"})))
    second = asyncio.run(chat_messages(FakeRequest(env, headers, body={"text": "Second"})))

    assert first.status_code == 200
    assert second.status_code == 200
    rows = db.connection.execute(
        "SELECT created_at, message_json FROM cf_chat_messages ORDER BY created_at, id"
    ).fetchall()
    assert len({row[0] for row in rows}) == 4
    assert [(json.loads(row[1])["sender"], json.loads(row[1])["text"]) for row in rows] == [
        ("human", "First"),
        ("ai", "Acknowledged"),
        ("human", "Second"),
        ("ai", "Acknowledged"),
    ]


def test_chat_fails_closed_for_auth_provider_and_d1_persistence_errors():
    secret = "chat-secret"
    headers = signed_headers(secret)

    unauthorized_env = type("Env", (), {"APP_DB": FakeDb(), "AI": FakeAi(), "INTERNAL_ASSERTION_SECRET": secret})()
    unauthorized = asyncio.run(chat_messages(FakeRequest(unauthorized_env)))
    assert unauthorized.status_code == 401

    provider_db = FakeDb()
    provider_env = type(
        "Env",
        (),
        {"APP_DB": provider_db, "AI": FakeAi(result={"unexpected": True}), "INTERNAL_ASSERTION_SECRET": secret},
    )()
    provider_error = asyncio.run(chat_messages(FakeRequest(provider_env, headers)))
    assert provider_error.status_code == 502
    assert provider_db.connection.execute("SELECT COUNT(*) FROM cf_chat_messages").fetchone()[0] == 0

    persistence_db = FakeDb(fail_batch=True)
    persistence_env = type(
        "Env",
        (),
        {"APP_DB": persistence_db, "AI": FakeAi(), "INTERNAL_ASSERTION_SECRET": secret},
    )()
    persistence_error = asyncio.run(chat_messages(FakeRequest(persistence_env, headers)))
    assert persistence_error.status_code == 503
    assert persistence_db.connection.execute("SELECT COUNT(*) FROM cf_chat_messages").fetchone()[0] == 0


def test_generate_reply_is_stateless_but_reserves_and_settles_chat_quota():
    secret = "chat-secret"
    db = FakeDb()
    ai = FakeAi(result={"response": "Friday works[1].", "usage": {"prompt_tokens": 12, "completion_tokens": 4}})
    env = type(
        "Env",
        (),
        {
            "APP_DB": db,
            "AI": ai,
            "INTERNAL_ASSERTION_SECRET": secret,
            "WORKERS_AI_CHAT_MODEL": "@cf/test/chat",
        },
    )()

    result = asyncio.run(
        generate_reply(
            FakeRequest(
                env,
                signed_headers(secret),
                body={
                    "text": "Draft a reply to Alice",
                    "history": [
                        {"sender": "human", "text": "Are we still on for Friday?"},
                        {"sender": "ai", "text": "I think so."},
                    ],
                },
            )
        )
    )

    assert result == {"text": "Friday works.", "app_id": None}
    assert ai.calls[0] == (
        "@cf/test/chat",
        {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are Omi, a concise and helpful personal assistant. Answer in the language used by the user. "
                        "Do not claim access to memories, files, apps, tools, or live information that was not supplied "
                        "in this chat."
                    ),
                },
                {"role": "user", "content": "Are we still on for Friday?"},
                {"role": "assistant", "content": "I think so."},
                {"role": "user", "content": "Draft a reply to Alice"},
            ],
            "stream": False,
            "max_tokens": 512,
            "temperature": 0.4,
        },
    )
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_messages").fetchone()[0] == 0
    quota = db.connection.execute(
        "SELECT source, message_id, chat_session_id, cost_usd, prompt_tokens, completion_tokens, model, "
        "settled_at IS NOT NULL AS settled FROM cf_chat_quota_events WHERE uid = 'chat-user'"
    ).fetchone()
    assert dict(quota) == {
        "source": "v2_chat_generate_reply",
        "message_id": quota[1],
        "chat_session_id": None,
        "cost_usd": 0.000001952,
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "model": "@cf/test/chat",
        "settled": 1,
    }


def test_generate_reply_rejects_bad_history_and_does_not_call_provider():
    secret = "chat-secret"
    db = FakeDb()
    ai = FakeAi()
    env = type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret})()

    response = asyncio.run(
        generate_reply(
            FakeRequest(
                env,
                signed_headers(secret),
                body={"text": "Draft", "history": [{"sender": "system", "text": "not allowed"}]},
            )
        )
    )

    assert response.status_code == 422
    assert ai.calls == []
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_quota_events").fetchone()[0] == 0


def test_generate_reply_marks_failed_provider_reservation_without_leaking_details():
    secret = "chat-secret"
    db = FakeDb()
    ai = FakeAi(error=RuntimeError("provider internals"))
    env = type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret})()

    response = asyncio.run(generate_reply(FakeRequest(env, signed_headers(secret))))

    assert response.status_code == 502
    assert json.loads(response.body) == {"error": "workers ai chat unavailable"}
    assert b"provider internals" not in response.body
    quota = db.connection.execute(
        "SELECT source, cost_usd, prompt_tokens, completion_tokens, settled_at IS NOT NULL AS settled "
        "FROM cf_chat_quota_events WHERE uid = 'chat-user'"
    ).fetchone()
    assert dict(quota) == {
        "source": "v2_chat_generate_reply",
        "cost_usd": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "settled": 1,
    }


def test_cloudflare_completion_uses_d1_session_quota_and_is_idempotent():
    secret = "chat-secret"
    db = FakeDb()
    ai = FakeAi(result={"response": "Workers answer", "usage": {"prompt_tokens": 9, "completion_tokens": 3}})
    env = type(
        "Env",
        (),
        {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret, "WORKERS_AI_CHAT_MODEL": "@cf/test/chat"},
    )()
    headers = {**signed_headers(secret), "idempotency-key": "compat-turn-1", "x-app-platform": "desktop"}
    body = {
        "model": "workers-ai",
        "messages": [{"role": "user", "content": "Can Workers host this?"}],
    }

    first = asyncio.run(cloudflare_chat_completions(FakeRequest(env, headers, body=body)))
    second = asyncio.run(cloudflare_chat_completions(FakeRequest(env, headers, body=body)))

    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = json.loads(first.body)
    second_payload = json.loads(second.body)
    assert first_payload["choices"][0]["message"] == {"role": "assistant", "content": "Workers answer"}
    assert second_payload["choices"][0]["message"] == first_payload["choices"][0]["message"]
    assert second_payload["usage"] == first_payload["usage"] == {
        "prompt_tokens": 9,
        "completion_tokens": 3,
        "total_tokens": 12,
    }
    assert len(ai.calls) == 1
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_sessions WHERE uid = 'chat-user'").fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_messages WHERE uid = 'chat-user'").fetchone()[0] == 2
    quota = db.connection.execute(
        "SELECT source, chat_session_id, settled_at IS NOT NULL AS settled FROM cf_chat_quota_events "
        "WHERE uid = 'chat-user'"
    ).fetchone()
    assert quota[0] == "cf_chat_completions"
    assert isinstance(quota[1], str) and quota[1]
    assert quota[2] == 1


def test_cloudflare_completion_preserves_d1_history_and_emits_openai_sse():
    secret = "chat-secret"
    db = FakeDb()
    db.connection.execute(
        "INSERT INTO cf_chat_sessions "
        "(uid,id,title,preview,created_at,updated_at,app_id,message_count,starred) "
        "VALUES ('chat-user','compat-session','New Chat',NULL,1,2,NULL,2,0)"
    )
    db.connection.executemany(
        "INSERT INTO cf_chat_messages (uid,id,app_id,created_at,message_json) VALUES (?, ?, NULL, ?, ?)",
        [
            stored("chat-user", "old-human", 1, "human", "Earlier question", "compat-session"),
            stored("chat-user", "old-ai", 2, "ai", "Earlier answer", "compat-session"),
        ],
    )
    db.connection.commit()
    ai = FakeAi(result={"response": "Follow-up", "usage": {"prompt_tokens": 18, "completion_tokens": 2}})
    env = type(
        "Env",
        (),
        {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret, "WORKERS_AI_CHAT_MODEL": "@cf/test/chat"},
    )()
    response = asyncio.run(
        cloudflare_chat_completions(
            FakeRequest(
                env,
                {**signed_headers(secret), "idempotency-key": "compat-stream-1"},
                body={
                    "messages": [{"role": "user", "content": "A follow-up"}],
                    "session_id": "compat-session",
                    "stream": True,
                },
            )
        )
    )
    body = asyncio.run(response_body(response)).decode()

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    assert "chat.completion.chunk" in body
    assert '"content":"Follow-up"' in body
    assert "data: [DONE]" in body
    assert "done:" not in body
    prompt = ai.calls[0][1]["messages"]
    assert {"role": "user", "content": "Earlier question"} in prompt
    assert {"role": "assistant", "content": "Earlier answer"} in prompt


def test_cloudflare_completion_rejects_legacy_tool_shape_before_provider():
    secret = "chat-secret"
    db = FakeDb()
    ai = FakeAi()
    env = type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret})()

    response = asyncio.run(
        cloudflare_chat_completions(
            FakeRequest(
                env,
                {**signed_headers(secret), "idempotency-key": "compat-invalid-1"},
                body={"messages": [{"role": "user", "content": "hello"}], "tools": []},
            )
        )
    )

    assert response.status_code == 400
    assert json.loads(response.body)["error"]["type"] == "invalid_request_error"
    assert ai.calls == []
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_messages").fetchone()[0] == 0
