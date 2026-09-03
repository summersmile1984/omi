import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from public_shared_chat_routes import (  # noqa: E402
    MAX_REQUEST_BODY_BYTES,
    MAX_TRANSCRIPT_CHARS,
    public_shared_conversation_chat,
)
from test_conversation_routes import FakeDb, insert_conversation  # noqa: E402


SECRET = "public-chat-test-secret"
PATH = "/v1/conversations/shared/chat"


class FakeRequest:
    def __init__(self, env, headers=None, body=None, *, content_length=None):
        self.scope = {"env": env}
        self.headers = dict(headers or {})
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self.method = "POST"
        self.url = SimpleNamespace(path=PATH)
        self._body = body if body is not None else {}
        self._content_length = content_length

    async def body(self):
        if isinstance(self._body, bytes):
            return self._body
        return json.dumps(self._body, ensure_ascii=False).encode()


class FakeAi:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        if self.error:
            raise self.error
        return {"response": self.response}


def signed_assertion(subject="a" * 64, *, secret=SECRET, path=PATH, method="POST", issued_at=None):
    issued_at = int(time.time()) if issued_at is None else issued_at
    payload = {
        "version": 1,
        "kind": "public-shared-chat",
        "subject": subject,
        "requestId": "public-chat-test",
        "audience": "api-core",
        "assertionId": "assertion-1",
        "issuedAt": issued_at,
        "expiresAt": issued_at + 60,
        "method": method,
        "path": path,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()).decode()
    return f"{encoded}.{signature.rstrip('=')}"


def environment(ai):
    db = FakeDb()
    insert_conversation(
        db,
        uid="conversation-owner",
        conversation_id="shared-1",
        created_at=100,
        visibility="public",
        transcript_segments=[
            {"text": "The owner plans a launch on Friday.", "is_user": True},
            {"text": "The team will meet at 10 AM.", "speaker_id": 2},
        ],
    )
    db.connection.execute(
        "INSERT INTO cf_shared_conversation_index (conversation_id, uid, visibility, updated_at) VALUES (?, ?, ?, ?)",
        ("shared-1", "conversation-owner", "public", 100),
    )
    db.connection.commit()
    return type("Env", (), {"APP_DB": db, "AI": ai, "INTERNAL_ASSERTION_SECRET": SECRET})()


def request(env, body=None, **kwargs):
    return FakeRequest(
        env,
        {"x-omi-public-chat-assertion": signed_assertion()},
        body or {"conversation_id": "shared-1", "question": "When is the launch?", "history": []},
        **kwargs,
    )


def test_public_chat_requires_the_independent_signed_assertion():
    ai = FakeAi("should not run")
    env = environment(ai)
    response = __import__("asyncio").run(
        public_shared_conversation_chat(
            FakeRequest(env, body={"conversation_id": "shared-1", "question": "hello", "history": []})
        )
    )
    assert response.status_code == 401
    assert ai.calls == []

    tampered = request(env)
    tampered.headers["x-omi-public-chat-assertion"] = signed_assertion(subject="b" * 64, secret="wrong-secret")
    response = __import__("asyncio").run(public_shared_conversation_chat(tampered))
    assert response.status_code == 401


def test_public_chat_reads_shared_d1_projection_and_calls_workers_ai():
    ai = FakeAi("Friday at 10 AM.")
    env = environment(ai)
    response = __import__("asyncio").run(public_shared_conversation_chat(request(env)))
    assert response.status_code == 200
    assert json.loads(response.body) == {"message": "Friday at 10 AM."}
    assert response.headers["cache-control"] == "no-store"
    assert ai.calls[0][1]["messages"][0]["role"] == "system"
    assert "The owner plans a launch on Friday." in ai.calls[0][1]["messages"][0]["content"]


def test_public_chat_hides_private_or_locked_conversations_and_bounds_body():
    ai = FakeAi("should not run")
    env = environment(ai)
    env.APP_DB.connection.execute(
        "UPDATE cf_conversations SET is_locked = 1 WHERE uid = ? AND id = ?", ("conversation-owner", "shared-1")
    )
    env.APP_DB.connection.commit()
    response = __import__("asyncio").run(public_shared_conversation_chat(request(env)))
    assert response.status_code == 404
    assert ai.calls == []

    large = FakeRequest(
        env,
        {"x-omi-public-chat-assertion": signed_assertion()},
        b"x" * (MAX_REQUEST_BODY_BYTES + 1),
        content_length=str(MAX_REQUEST_BODY_BYTES + 1),
    )
    response = __import__("asyncio").run(public_shared_conversation_chat(large))
    assert response.status_code == 413


def test_public_chat_bounds_transcript_and_fails_closed_on_workers_ai_fault():
    ai = FakeAi(error=RuntimeError("offline"))
    env = environment(ai)
    env.APP_DB.connection.execute(
        "UPDATE cf_conversations SET transcript_segments_json = ? WHERE uid = ? AND id = ?",
        (json.dumps([{"text": "first"}] + [{"text": "x" * 1_000}] * 40), "conversation-owner", "shared-1"),
    )
    env.APP_DB.connection.commit()
    response = __import__("asyncio").run(public_shared_conversation_chat(request(env)))
    assert response.status_code == 503
    assert len(ai.calls[0][1]["messages"][0]["content"]) <= MAX_TRANSCRIPT_CHARS + 400
