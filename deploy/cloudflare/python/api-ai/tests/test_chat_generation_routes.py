import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from chat_generation_routes import chat_messages  # noqa: E402


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
    def __init__(self, *, fail_batch=False):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript(
            "CREATE TABLE cf_account_deletion_intents (uid TEXT PRIMARY KEY);"
            "CREATE TABLE cf_account_deletion_tombstones (uid TEXT PRIMARY KEY);"
        )
        self.connection.executescript((migration_dir / "0042_chat_messages.sql").read_text())
        self.connection.executescript((migration_dir / "0054_chat_sessions.sql").read_text())
        self.fail_batch = fail_batch

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

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
        self.result = result if result is not None else {"response": "Hello\nthere"}
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


def signed_headers(secret: str, uid: str = "chat-user"):
    raw = json.dumps({"uid": uid}, separators=(",", ":")).encode()
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


def stored(uid: str, message_id: str, created_at: int, sender: str, text: str):
    return (
        uid,
        message_id,
        created_at,
        json.dumps(
            {
                "id": message_id,
                "sender": sender,
                "text": text,
                "chat_session_id": "session-1",
                "session_id": "session-1",
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


def test_rapid_sequential_exchanges_keep_strict_history_order(monkeypatch):
    secret = "chat-secret"
    db = FakeDb()
    ai = FakeAi(result={"response": "Acknowledged"})
    env = type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    times = iter((1_000.0, 1_000.000001, 1_000.0, 1_000.000002, 1_000.0))
    monkeypatch.setattr("chat_generation_routes.time.time", lambda: next(times))

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
