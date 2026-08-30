import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from chat_first_routes import validate_chat_first_blocks  # noqa: E402


class FakeDb:
    def __init__(self, *, broken: bool = False):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for name in (
            "0016_action_items.sql",
            "0018_goals.sql",
            "0032_conversations.sql",
            "0034_account_cutover.sql",
            "0037_memories.sql",
        ):
            self.connection.executescript((migration_dir / name).read_text())
        self.broken = broken

    def prepare(self, sql):
        if self.broken:
            raise RuntimeError("D1 unavailable")
        return FakeStatement(self.connection, sql)


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


class FakeRequest:
    def __init__(self, env, headers, body):
        self.scope = {"env": env}
        self.headers = headers
        self._body = body

    async def body(self):
        return json.dumps(self._body).encode()


def signed_headers(secret: str, uid: str = "chat-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "chat-first-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(secret: str, *, broken: bool = False):
    db = FakeDb(broken=broken)
    db.connection.execute(
        "INSERT INTO cf_account_cutover "
        "(uid, state, account_generation, checkpoint_phase, manifest_id, destination_backend_bound, updated_at) "
        "VALUES (?, 'new', 3, 'completed', 'isolated-staging-v1', 1, 1)",
        ("chat-user",),
    )
    db.connection.execute(
        "INSERT INTO cf_action_items "
        "(uid, id, description, status, created_at, updated_at) VALUES (?, ?, ?, 'active', 1, 1)",
        ("chat-user", "task-1", "Call Alice"),
    )
    db.connection.execute(
        "INSERT INTO cf_goals "
        "(uid, id, title, desired_outcome, status, source, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'focused', 'user', 1, 1)",
        ("chat-user", "goal-1", "Ship feature", "Ship feature"),
    )
    db.connection.commit()
    return type(
        "Env",
        (),
        {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret, "ACCOUNT_CUTOVER_PROFILE": "isolated-staging"},
    )()


def request_body(*, generation: int = 3, owner: str = "chat-user", blocks=None):
    return {
        "source_surface": "main_chat",
        "control_generation": generation,
        "owner_fence": owner,
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "blocks": blocks or [{"type": "taskCard", "task_id": "task-1"}],
    }


def test_chat_first_validation_checks_d1_entities_and_is_retry_stable():
    secret = "chat-first-secret"
    env = make_env(secret)
    first = asyncio.run(validate_chat_first_blocks(FakeRequest(env, signed_headers(secret), request_body())))
    second = asyncio.run(validate_chat_first_blocks(FakeRequest(env, signed_headers(secret), request_body())))

    assert first == second
    assert first.accepted is True
    assert first.code == "accepted"
    assert first.blocks[0]["id"].startswith("cfb_")
    assert first.blocks[0]["task_id"] == "task-1"


def test_chat_first_validation_fails_closed_for_auth_generation_entity_and_d1_errors():
    secret = "chat-first-secret"
    env = make_env(secret)
    assert asyncio.run(validate_chat_first_blocks(FakeRequest(env, {}, request_body()))).status_code == 401
    invalid = asyncio.run(validate_chat_first_blocks(FakeRequest(env, signed_headers(secret), {"blocks": []})))
    assert invalid.code == "invalid_request"
    mismatch = asyncio.run(
        validate_chat_first_blocks(FakeRequest(env, signed_headers(secret), request_body(generation=2)))
    )
    assert mismatch.code == "generation_mismatch"
    missing = asyncio.run(
        validate_chat_first_blocks(
            FakeRequest(
                env,
                signed_headers(secret),
                request_body(blocks=[{"type": "goalLink", "goal_id": "missing", "summary": "Missing"}]),
            )
        )
    )
    assert missing.code == "entity_unavailable"
    broken = asyncio.run(
        validate_chat_first_blocks(FakeRequest(make_env(secret, broken=True), signed_headers(secret), request_body()))
    )
    assert broken.status_code == 503


def test_chat_first_validation_rejects_cold_start_and_cross_account_owner():
    secret = "chat-first-secret"
    env = make_env(secret)
    cold_start = asyncio.run(
        validate_chat_first_blocks(
            FakeRequest(
                env,
                signed_headers(secret),
                request_body(
                    blocks=[
                        {
                            "type": "questionCard",
                            "question_id": "question-1",
                            "text": "What next?",
                            "subject": {"kind": "cold_start", "id": "sequence-1"},
                            "options": [
                                {"option_id": "option-1", "label": "Now", "prepared_answer": "Now"},
                            ],
                            "cold_start_sequence": {"sequence_id": "sequence-1", "step": 1},
                        }
                    ]
                ),
            )
        )
    )
    assert cold_start.code == "entity_unavailable"
    assert (
        asyncio.run(
            validate_chat_first_blocks(FakeRequest(env, signed_headers(secret), request_body(owner="other-user")))
        ).code
        == "capability_unavailable"
    )
