import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from migration_routes import get_migration_requests  # noqa: E402


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
    def __init__(self, env, headers=None, query=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.query_params = query or {}


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
