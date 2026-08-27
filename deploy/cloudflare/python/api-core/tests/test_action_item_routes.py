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
    batch_router,
    batch_create_action_items,
    batch_update_action_items,
    create_action_item,
    delete_action_item,
    get_action_item,
    get_pending_sync_items,
    list_action_item_ids,
    list_action_items,
    sync_batch_update,
    toggle_action_item_completion,
    update_action_item,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration = Path(__file__).parents[3] / "migrations/app/0016_action_items.sql"
        self.connection.executescript(migration.read_text())

    def prepare(self, sql):
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

    async def all(self):
        rows = self.connection.execute(self.sql, self.args).fetchall()
        return {"results": [dict(row) for row in rows]}

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class FakeRequest:
    def __init__(self, env, headers, body=None, query=None):
        self.scope = {"env": env}
        self.headers = headers
        self.body = body
        self.query_params = query or {}

    async def json(self):
        return self.body


def signed_headers(secret: str, uid: str = "action-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "action-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def test_action_item_crud_is_uid_scoped_and_idempotent():
    secret = "action-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)

    invalid = asyncio.run(create_action_item(FakeRequest(env, headers, {"description": ""})))
    assert invalid.status_code == 400

    created = asyncio.run(
        create_action_item(
            FakeRequest(
                env,
                headers,
                {
                    "description": "  Send weekly recap  ",
                    "conversation_id": "conversation-1",
                    "due_at": "2026-08-30T10:00:00Z",
                    "provenance": [{"kind": "conversation", "id": "conversation-1"}],
                },
            )
        )
    )
    assert created["description"] == "  Send weekly recap  "
    assert created["status"] == "active"
    assert created["completed"] is False
    assert created["due_at"] == "2026-08-30T10:00:00+00:00"

    retry = asyncio.run(create_action_item(FakeRequest(env, headers, {"description": "send weekly recap"})))
    assert retry["id"] == created["id"]

    listed = asyncio.run(list_action_items(FakeRequest(env, headers, query={"limit": "50"})))
    assert listed["has_more"] is False
    assert [item["id"] for item in listed["action_items"]] == [created["id"]]

    invalid_range = asyncio.run(
        list_action_items(
            FakeRequest(
                env,
                headers,
                query={"start_date": "2026-09-02T00:00:00Z", "end_date": "2026-09-01T00:00:00Z"},
            )
        )
    )
    assert invalid_range.status_code == 400

    updated = asyncio.run(update_action_item(FakeRequest(env, headers, {"completed": True}), created["id"]))
    assert updated["status"] == "completed"
    assert updated["completed"] is True
    assert updated["completed_at"] is not None

    ids = asyncio.run(list_action_item_ids(FakeRequest(env, headers, query={"completed": "true"})))
    assert ids == {"ids": [created["id"]], "completed_scope": True}

    other_headers = signed_headers(secret, uid="other-user")
    cross_user = asyncio.run(get_action_item(FakeRequest(env, other_headers), created["id"]))
    assert cross_user.status_code == 404

    deleted = asyncio.run(delete_action_item(FakeRequest(env, headers), created["id"]))
    assert deleted.status_code == 204
    missing = asyncio.run(get_action_item(FakeRequest(env, headers), created["id"]))
    assert missing.status_code == 404


def test_action_item_batch_route_is_registered_before_dynamic_id_route():
    assert [route.path for route in batch_router.routes] == [
        "/v1/action-items/batch",
        "/v1/action-items/sync-batch",
        "/v1/action-items/batch",
        "/v1/action-items/batch-delete",
    ]


def test_action_item_batch_update_returns_missing_ids():
    secret = "action-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    created = asyncio.run(create_action_item(FakeRequest(env, headers, {"description": "Reorder agenda"})))

    result = asyncio.run(
        batch_update_action_items(
            FakeRequest(
                env,
                headers,
                {
                    "items": [
                        {"id": created["id"], "sort_order": 2, "indent_level": 1},
                        {"id": "missing-action-item", "sort_order": 3},
                    ]
                },
            )
        )
    )
    assert result == {
        "status": "ok",
        "updated_count": 1,
        "updated_ids": [created["id"]],
        "missing_ids": ["missing-action-item"],
        "noop_ids": [],
    }

    updated = asyncio.run(get_action_item(FakeRequest(env, headers), created["id"]))
    assert updated["sort_order"] == 2
    assert updated["indent_level"] == 1

    toggled = asyncio.run(
        toggle_action_item_completion(
            FakeRequest(env, headers, query={"completed": "false"}),
            created["id"],
        )
    )
    assert toggled["completed"] is False


def test_action_item_batch_create_preserves_order_and_idempotency():
    secret = "action-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    body = [
        {"description": "First batch item", "sort_order": 1},
        {"description": "Second batch item", "completed": True},
    ]

    created = asyncio.run(batch_create_action_items(FakeRequest(env, headers, body)))
    assert created["created_count"] == 2
    assert [item["description"] for item in created["action_items"]] == [
        "First batch item",
        "Second batch item",
    ]
    assert created["action_items"][1]["completed"] is True

    retry = asyncio.run(batch_create_action_items(FakeRequest(env, headers, body)))
    assert [item["id"] for item in retry["action_items"] if item["description"] == "First batch item"] == [
        created["action_items"][0]["id"]
    ]
    assert retry["created_count"] == 2

    invalid = asyncio.run(batch_create_action_items(FakeRequest(env, headers, [{"description": ""}])))
    assert invalid.status_code == 400


def test_reminders_sync_projection_and_batch_update_are_d1_backed():
    secret = "action-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    created = asyncio.run(create_action_item(FakeRequest(env, headers, {"description": "Sync the reminders"})))
    asyncio.run(
        env.APP_DB.prepare("UPDATE cf_action_items SET sync_requested = 1 WHERE uid = ? AND id = ?")
        .bind("action-user", created["id"])
        .run()
    )

    pending = asyncio.run(get_pending_sync_items(FakeRequest(env, headers)))
    assert [item["id"] for item in pending["pending_export"]] == [created["id"]]
    assert pending["synced_items"] == []

    result = asyncio.run(
        sync_batch_update(
            FakeRequest(
                env,
                headers,
                {
                    "items": [
                        {
                            "id": created["id"],
                            "description": "Sync the reminders now",
                            "exported": True,
                            "export_platform": "apple_reminders",
                            "apple_reminder_id": "reminder-1",
                        },
                        {"id": "missing-item", "exported": True},
                    ]
                },
            )
        )
    )
    assert result == {
        "status": "ok",
        "updated_count": 1,
        "updated_ids": [created["id"]],
        "missing_ids": ["missing-item"],
        "locked_ids": [],
        "noop_ids": [],
    }
    synced = asyncio.run(get_pending_sync_items(FakeRequest(env, headers)))
    assert synced["pending_export"] == []
    assert synced["synced_items"][0]["apple_reminder_id"] == "reminder-1"
    assert synced["synced_items"][0]["description"] == "Sync the reminders now"

    invalid = asyncio.run(sync_batch_update(FakeRequest(env, headers, {"items": [{"id": ""}]})))
    assert invalid.status_code == 400
