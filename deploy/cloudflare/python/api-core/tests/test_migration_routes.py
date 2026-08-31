import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from migration_routes import (  # noqa: E402
    finalize_migration_request,
    get_migration_requests,
    handle_batch_migration_requests,
    handle_migration_requests,
)


class FakeStatement:
    def __init__(self, connection, sql):
        self.connection = connection
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def all(self):
        rows = self.connection.execute(self.sql, self.args).fetchall()
        return {"results": [dict(row) for row in rows]}

    async def first(self):
        row = self.connection.execute(self.sql, self.args).fetchone()
        return dict(row) if row is not None else None


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for name in ("0032_conversations.sql", "0037_memories.sql", "0042_chat_messages.sql"):
            self.connection.executescript((migration_dir / name).read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeRequest:
    def __init__(self, env, headers=None, query=None, payload=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.query_params = query or {}
        self._payload = payload

    async def body(self):
        return json.dumps(self._payload).encode() if self._payload is not None else b""


def signed_headers(secret: str, uid: str = "migration-user"):
    raw = json.dumps({"uid": uid, "authority": "better-auth"}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(secret="migration-secret"):
    return type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()


class ShadowDb(FakeDb):
    def __init__(self):
        super().__init__()
        self.connection.executescript(
            """
            CREATE TABLE cf_account_cutover (
              uid TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, state TEXT NOT NULL,
              account_generation INTEGER NOT NULL, checkpoint_phase TEXT NOT NULL,
              destination_backend_bound INTEGER NOT NULL
            );
            CREATE TABLE cf_account_deletion_intents (uid TEXT PRIMARY KEY);
            CREATE TABLE cf_account_deletion_tombstones (
              uid TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
            );
            """
        )
        migration = Path(__file__).parents[3] / "migrations/app/0103_data_protection_migration.sql"
        self.connection.executescript(migration.read_text())


def make_shadow_env(secret="migration-secret"):
    return type("Env", (), {"APP_DB": ShadowDb(), "INTERNAL_ASSERTION_SECRET": secret})()


def shadow_headers(secret: str, generation: int = 0, *, key: str = "migration-run-1"):
    headers = signed_headers(secret)
    headers["idempotency-key"] = key
    headers["x-account-generation"] = str(generation)
    return headers


def populate_ready_authority(env, *, generation: int = 0):
    db = env.APP_DB.connection
    db.execute(
        "INSERT INTO cf_account_cutover "
        "(uid, schema_version, state, account_generation, checkpoint_phase, destination_backend_bound) "
        "VALUES (?, 1, 'new', ?, 'completed', 1)",
        ("migration-user", generation),
    )
    db.execute(
        "INSERT INTO cf_data_protection_migration_control "
        "(uid, source, enabled, executor_state, account_generation, source_revision, updated_at) "
        "VALUES (?, 'cloudflare_data_protection_projection', 1, 'ready', ?, 'test-revision', 1)",
        ("migration-user", generation),
    )
    db.commit()


def test_migration_inventory_requires_auth_and_target_level():
    env = make_env()
    assert asyncio.run(get_migration_requests(FakeRequest(env))).status_code == 401
    invalid = asyncio.run(
        get_migration_requests(FakeRequest(env, signed_headers("migration-secret"), {"target_level": "standard"}))
    )
    assert invalid.status_code == 400
    assert json.loads(invalid.body)["detail"].startswith("Invalid target_level")


def test_migration_inventory_matches_legacy_order_and_skips_public_conversations():
    env = make_env()
    db = env.APP_DB.connection
    db.execute(
        "INSERT INTO cf_conversations (uid, id, created_at, visibility) VALUES (?, ?, ?, ?)",
        ("migration-user", "private-conversation", 1, "private"),
    )
    db.execute(
        "INSERT INTO cf_conversations (uid, id, created_at, visibility) VALUES (?, ?, ?, ?)",
        ("migration-user", "shared-conversation", 2, "shared"),
    )
    db.execute(
        "INSERT INTO cf_memories (uid, id, content, memory_tier, valid_at, created_at, updated_at, data_protection_level) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("migration-user", "enhanced-memory", "done", "long_term", 1, 1, 1, "enhanced"),
    )
    db.execute(
        "INSERT INTO cf_memories (uid, id, content, memory_tier, valid_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("migration-user", "standard-memory", "todo", "long_term", 1, 1, 1),
    )
    db.execute(
        "INSERT INTO cf_chat_messages (uid, id, created_at, message_json) VALUES (?, ?, ?, ?)",
        ("migration-user", "chat-message", 1, "{}"),
    )
    db.commit()

    result = asyncio.run(
        get_migration_requests(FakeRequest(env, signed_headers("migration-secret"), {"target_level": "enhanced"}))
    )
    assert result == {
        "needs_migration": [
            {"id": "private-conversation", "type": "conversation"},
            {"id": "standard-memory", "type": "memory"},
            {"id": "chat-message", "type": "chat"},
        ]
    }


def test_migration_write_shadow_fails_closed_without_authority_and_receipt():
    env = make_shadow_env()
    request = FakeRequest(
        env,
        shadow_headers("migration-secret"),
        payload={"type": "memory", "id": "memory-1", "target_level": "enhanced"},
    )

    response = asyncio.run(handle_migration_requests(request))

    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "migration unavailable", "reason": "missing_completed_cutover"}
    count = env.APP_DB.connection.execute("SELECT COUNT(*) FROM cf_data_protection_migration_runs").fetchone()[0]
    assert count == 0


def test_migration_write_shadow_requires_idempotency_and_rejects_generation_reuse():
    env = make_shadow_env()
    populate_ready_authority(env, generation=4)
    valid_payload = {"target_level": "enhanced"}

    missing_headers = asyncio.run(
        handle_migration_requests(FakeRequest(env, signed_headers("migration-secret"), payload=valid_payload))
    )
    assert missing_headers.status_code == 400

    mismatched_generation = asyncio.run(
        handle_migration_requests(
            FakeRequest(
                env,
                shadow_headers("migration-secret", generation=3),
                payload=valid_payload,
            )
        )
    )
    assert mismatched_generation.status_code == 409
    assert json.loads(mismatched_generation.body) == {"error": "account generation mismatch"}


def test_migration_write_shadow_rejects_batch_finalize_and_deletion_fence():
    env = make_shadow_env()
    populate_ready_authority(env)
    batch = asyncio.run(
        handle_batch_migration_requests(
            FakeRequest(
                env,
                shadow_headers("migration-secret", key="batch-1"),
                payload={
                    "requests": [
                        {"type": "conversation", "id": "conversation-1", "target_level": "enhanced"},
                        {"type": "chat", "id": "chat-1", "target_level": "enhanced"},
                    ]
                },
            )
        )
    )
    assert batch.status_code == 503
    assert json.loads(batch.body)["reason"] == "encryption_executor_unavailable"

    finalize = asyncio.run(
        finalize_migration_request(
            FakeRequest(
                env,
                shadow_headers("migration-secret", key="finalize-1"),
                payload={"target_level": "enhanced"},
            )
        )
    )
    assert finalize.status_code == 503
    assert json.loads(finalize.body)["reason"] == "encryption_executor_unavailable"

    env.APP_DB.connection.execute(
        "INSERT INTO cf_account_deletion_intents (uid) VALUES (?)", ("migration-user",)
    )
    env.APP_DB.connection.commit()
    fenced = asyncio.run(
        handle_migration_requests(
            FakeRequest(
                env,
                shadow_headers("migration-secret", key="fenced-1"),
                payload={"target_level": "enhanced"},
            )
        )
    )
    assert fenced.status_code == 409
    assert json.loads(fenced.body) == {"error": "account deletion in progress"}


def test_migration_write_shadow_preserves_invalid_target_error():
    env = make_shadow_env()
    response = asyncio.run(
        finalize_migration_request(
            FakeRequest(
                env,
                signed_headers("migration-secret"),
                payload={"target_level": "standard"},
            )
        )
    )
    assert response.status_code == 400
    assert json.loads(response.body)["detail"] == "Invalid target_level. Only migration to 'enhanced' is supported."


def test_migration_receipt_schema_has_deletion_fence():
    env = make_shadow_env()
    db = env.APP_DB.connection
    db.execute(
        "INSERT INTO cf_account_deletion_tombstones (uid, expires_at) VALUES (?, ?)",
        ("fenced-user", 4_000_000_000),
    )
    with pytest.raises(sqlite3.IntegrityError, match="account deletion fence"):
        db.execute(
            "INSERT INTO cf_data_protection_migration_control "
            "(uid, source, enabled, executor_state, account_generation, source_revision, updated_at) "
            "VALUES (?, 'cloudflare_data_protection_projection', 1, 'ready', 0, 'revision', 1)",
            ("fenced-user",),
        )
