import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from action_item_routes import (  # noqa: E402
    accept_shared_action_items,
    get_action_item,
    get_shared_action_items,
    share_action_items,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0016_action_items.sql").read_text())
        self.connection.executescript((migration_dir / "0043_task_shares.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        results = []
        try:
            self.connection.execute("BEGIN")
            for statement in statements:
                cursor = self.connection.execute(statement.sql, statement.args)
                results.append({"meta": {"changes": cursor.rowcount}})
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return results


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


class FakeRequest:
    def __init__(self, env, headers=None, body=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.body = body
        self.query_params = {}

    async def json(self):
        return self.body


def signed_headers(secret: str, uid: str, display_name: str | None = None):
    context = {
        "uid": uid,
        "authority": "better-auth",
        "requestId": "task-share-test",
    }
    if display_name is not None:
        context["displayName"] = display_name
    raw = json.dumps(context, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def insert_item(db: FakeDb, uid: str, item_id: str, description: str, *, locked: bool = False):
    db.connection.execute(
        "INSERT INTO cf_action_items "
        "(uid, id, description, status, completed, owner, due_at, source, provenance_json, is_locked, "
        "exported, created_at, updated_at, sync_requested, deleted) "
        "VALUES (?, ?, ?, 'active', 0, 'user', 1788105600, 'manual', '[]', ?, 0, 100, 100, 0, 0)",
        (uid, item_id, description, 1 if locked else 0),
    )
    db.connection.commit()


def test_task_share_preview_acceptance_and_duplicate_claim_are_d1_owned():
    secret = "share-secret"
    db = FakeDb()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()
    insert_item(db, "sender", "task-1", "Ship the Worker migration")

    shared = asyncio.run(
        share_action_items(
            FakeRequest(
                env,
                signed_headers(secret, "sender", "Alice"),
                {"task_ids": ["task-1"]},
            )
        )
    )
    assert shared["url"] == f"https://h.omi.me/tasks/{shared['token']}"

    preview = asyncio.run(get_shared_action_items(FakeRequest(env), shared["token"]))
    assert preview == {
        "sender_name": "Alice",
        "tasks": [
            {
                "description": "Ship the Worker migration",
                "due_at": "2026-08-30T16:00:00+00:00",
            }
        ],
        "count": 1,
    }

    accepted = asyncio.run(
        accept_shared_action_items(
            FakeRequest(env, signed_headers(secret, "recipient", "Bob"), {"token": shared["token"]})
        )
    )
    assert accepted["count"] == 1
    copied = asyncio.run(get_action_item(FakeRequest(env, signed_headers(secret, "recipient")), accepted["created"][0]))
    assert copied["description"] == "Ship the Worker migration"
    assert copied["source"] == "shared"
    assert copied["shared_from"] == {
        "token": shared["token"],
        "sender_uid": "sender",
        "sender_name": "Alice",
        "original_task_id": "task-1",
    }

    duplicate = asyncio.run(
        accept_shared_action_items(FakeRequest(env, signed_headers(secret, "recipient"), {"token": shared["token"]}))
    )
    assert duplicate.status_code == 409
    copied_count = db.connection.execute("SELECT COUNT(*) FROM cf_action_items WHERE uid = 'recipient'").fetchone()[0]
    assert copied_count == 1


def test_task_share_rejects_locked_missing_self_and_expired_paths():
    secret = "share-secret"
    db = FakeDb()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()
    sender = signed_headers(secret, "sender", "Alice")
    insert_item(db, "sender", "unlocked", "Visible")
    insert_item(db, "sender", "locked", "Private", locked=True)

    missing = asyncio.run(share_action_items(FakeRequest(env, sender, {"task_ids": ["missing"]})))
    assert missing.status_code == 404
    locked = asyncio.run(share_action_items(FakeRequest(env, sender, {"task_ids": ["locked"]})))
    assert locked.status_code == 402

    shared = asyncio.run(share_action_items(FakeRequest(env, sender, {"task_ids": ["unlocked"]})))
    self_accept = asyncio.run(accept_shared_action_items(FakeRequest(env, sender, {"token": shared["token"]})))
    assert self_accept.status_code == 400

    db.connection.execute("UPDATE cf_action_items SET is_locked = 1 WHERE uid = 'sender' AND id = 'unlocked'")
    db.connection.commit()
    all_locked = asyncio.run(
        accept_shared_action_items(FakeRequest(env, signed_headers(secret, "recipient"), {"token": shared["token"]}))
    )
    assert all_locked.status_code == 402
    assert db.connection.execute("SELECT COUNT(*) FROM cf_task_share_acceptances").fetchone()[0] == 0

    db.connection.execute("UPDATE cf_task_shares SET expires_at = 0 WHERE token = ?", (shared["token"],))
    db.connection.commit()
    expired = asyncio.run(get_shared_action_items(FakeRequest(env), shared["token"]))
    assert expired.status_code == 404
