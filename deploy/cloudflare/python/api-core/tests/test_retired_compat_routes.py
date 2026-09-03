import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from retired_compat_routes import (  # noqa: E402
    delete_limitless_conversations,
    migrate_ai_tasks,
    migrate_conversation_items,
    restore_legacy_conversation_items,
)


class FakeRequest:
    def __init__(self, env, headers=None, query_params=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.query_params = query_params or {}


def signed_headers(secret: str, uid: str = "compat-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "compat-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(secret: str):
    return type("Env", (), {"INTERNAL_ASSERTION_SECRET": secret})()


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
        result = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return type("Result", (), {"meta": {"changes": result.rowcount}})()


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
        for statement in statements:
            self.connection.execute(statement.sql, statement.args)
        self.connection.commit()


def make_conversation_env(secret: str):
    database = FakeDb()
    database.connection.execute(
        "INSERT INTO cf_conversations (uid, id, created_at, updated_at, source) " "VALUES (?, ?, ?, ?, 'limitless')",
        ("compat-user", "limitless-1", 100, 100),
    )
    database.connection.execute(
        "INSERT INTO cf_conversations (uid, id, created_at, updated_at, source) " "VALUES (?, ?, ?, ?, 'omi')",
        ("compat-user", "omi-1", 101, 101),
    )
    database.connection.execute(
        "INSERT INTO cf_conversations (uid, id, created_at, updated_at, source) " "VALUES (?, ?, ?, ?, 'limitless')",
        ("other-user", "limitless-2", 102, 102),
    )
    database.connection.execute(
        "INSERT INTO cf_shared_conversation_index (conversation_id, uid, visibility, updated_at) "
        "VALUES (?, ?, 'public', ?)",
        ("limitless-1", "compat-user", 100),
    )
    database.connection.commit()
    return (
        type(
            "Env",
            (),
            {"INTERNAL_ASSERTION_SECRET": secret, "APP_DB": database},
        )(),
        database,
    )


def test_retired_routes_preserve_inert_response_envelopes():
    secret = "compat-secret"
    env = make_env(secret)
    headers = signed_headers(secret)

    assert asyncio.run(migrate_ai_tasks(FakeRequest(env, headers))) == {
        "status": "legacy task migration retired; no action taken"
    }
    assert asyncio.run(migrate_conversation_items(FakeRequest(env, headers, {"limit": "100"}))) == {
        "status": "ok",
        "migrated": 0,
        "deleted": 0,
        "restored": 0,
        "skipped_existing": 0,
        "has_more": False,
        "next_cursor": None,
    }
    assert asyncio.run(restore_legacy_conversation_items(FakeRequest(env, headers, {"cursor": "page-1"}))) == {
        "status": "ok",
        "restored": 0,
        "skipped_existing": 0,
        "has_more": False,
        "next_cursor": None,
    }
    unavailable = asyncio.run(delete_limitless_conversations(FakeRequest(env, headers)))
    assert unavailable.status_code == 503


def test_limitless_delete_removes_only_imported_rows_and_queues_vector_deletes():
    secret = "compat-secret"
    env, database = make_conversation_env(secret)
    result = asyncio.run(delete_limitless_conversations(FakeRequest(env, signed_headers(secret))))
    assert result == {
        "deleted_count": 1,
        "message": "Successfully deleted 1 Limitless conversations",
    }
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM cf_conversations WHERE uid = 'compat-user' AND source = 'limitless'"
        ).fetchone()[0]
        == 0
    )
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM cf_conversations WHERE uid = 'compat-user' AND source = 'omi'"
        ).fetchone()[0]
        == 1
    )
    assert (
        database.connection.execute("SELECT COUNT(*) FROM cf_conversations WHERE uid = 'other-user'").fetchone()[0] == 1
    )
    vector_row = database.connection.execute(
        "SELECT operation, source_id FROM cf_vector_projection_outbox WHERE uid = 'compat-user'"
    ).fetchone()
    assert tuple(vector_row) == ("delete", "limitless-1")
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM cf_shared_conversation_index WHERE uid = 'compat-user'"
        ).fetchone()[0]
        == 0
    )
    database.connection.close()


def test_limitless_delete_honors_account_deletion_fence():
    secret = "compat-secret"
    env, database = make_conversation_env(secret)
    database.connection.execute(
        "INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) "
        "VALUES (?, ?, 'pending', 'quiescing', ?, ?, ?)",
        ("compat-user", "delete-job", 200, 200, 200),
    )
    database.connection.commit()
    result = asyncio.run(delete_limitless_conversations(FakeRequest(env, signed_headers(secret))))
    assert result.status_code == 409
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM cf_conversations WHERE uid = 'compat-user' AND source = 'limitless'"
        ).fetchone()[0]
        == 1
    )
    database.connection.close()


def test_retired_routes_fail_closed_and_bound_pagination():
    secret = "compat-secret"
    env = make_env(secret)

    unauthorized = asyncio.run(migrate_ai_tasks(FakeRequest(env)))
    assert unauthorized.status_code == 401

    invalid_limit = asyncio.run(migrate_conversation_items(FakeRequest(env, signed_headers(secret), {"limit": "101"})))
    assert invalid_limit.status_code == 422

    invalid_cursor = asyncio.run(
        restore_legacy_conversation_items(FakeRequest(env, signed_headers(secret), {"cursor": ""}))
    )
    assert invalid_cursor.status_code == 422

    invalid_limitless_auth = asyncio.run(delete_limitless_conversations(FakeRequest(env)))
    assert invalid_limitless_auth.status_code == 401
