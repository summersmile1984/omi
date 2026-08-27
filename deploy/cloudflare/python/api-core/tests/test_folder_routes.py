import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from folder_routes import (  # noqa: E402
    create_folder,
    delete_folder,
    get_folder,
    list_folder_conversations,
    list_folders,
    move_conversation_to_folder,
    reorder_folders,
    update_folder,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0019_folders.sql").read_text())
        self.connection.executescript((migration_dir / "0032_conversations.sql").read_text())
        self.connection.executescript((migration_dir / "0033_conversation_sync_flag.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        self.connection.execute("BEGIN")
        try:
            for statement in statements:
                self.connection.execute(statement.sql, statement.args)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


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


def signed_headers(secret: str, uid: str = "folder-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "folder-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def test_folder_metadata_initializes_system_folders_and_supports_crud():
    secret = "folder-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)

    initial = asyncio.run(list_folders(FakeRequest(env, headers)))
    assert [folder["category_mapping"] for folder in initial] == ["work", "personal", "social"]
    assert all(folder["is_system"] for folder in initial)

    created = asyncio.run(
        create_folder(
            FakeRequest(
                env,
                headers,
                {"name": "Research", "description": "Research notes", "color": "#334155", "icon": "🔬"},
            )
        )
    )
    assert created["name"] == "Research"
    assert created["order"] == 3
    assert created["is_system"] is False

    fetched = asyncio.run(get_folder(FakeRequest(env, headers), created["id"]))
    assert fetched == created
    updated = asyncio.run(
        update_folder(FakeRequest(env, headers, {"name": "Research & Notes", "order": 1}), created["id"])
    )
    assert updated["name"] == "Research & Notes"
    assert updated["order"] == 1

    folders = asyncio.run(list_folders(FakeRequest(env, headers)))
    reordered_ids = [created["id"], *[folder["id"] for folder in folders if folder["id"] != created["id"]]]
    assert asyncio.run(reorder_folders(FakeRequest(env, headers, {"folder_ids": reordered_ids}))) == {"status": "ok"}
    assert asyncio.run(list_folders(FakeRequest(env, headers)))[0]["id"] == created["id"]

    deleted = asyncio.run(delete_folder(FakeRequest(env, headers), created["id"]))
    assert deleted.status_code == 204
    missing = asyncio.run(get_folder(FakeRequest(env, headers), created["id"]))
    assert missing.status_code == 404


def test_folder_routes_enforce_system_and_uid_boundaries():
    secret = "folder-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    system = asyncio.run(list_folders(FakeRequest(env, headers)))[0]

    cannot_delete = asyncio.run(delete_folder(FakeRequest(env, headers), system["id"]))
    assert cannot_delete.status_code == 400
    invalid_order = asyncio.run(
        reorder_folders(FakeRequest(env, headers, {"folder_ids": [system["id"], system["id"]]}))
    )
    assert invalid_order.status_code == 400
    unknown_order = asyncio.run(reorder_folders(FakeRequest(env, headers, {"folder_ids": ["missing-folder"]})))
    assert unknown_order.status_code == 422

    other = asyncio.run(get_folder(FakeRequest(env, signed_headers(secret, "other-user")), system["id"]))
    assert other.status_code == 404


def test_folder_conversation_projection_lists_and_moves_with_count_refresh():
    secret = "folder-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    folders = asyncio.run(list_folders(FakeRequest(env, headers)))
    source_folder = folders[0]
    target_folder = asyncio.run(create_folder(FakeRequest(env, headers, {"name": "Research"})))
    env.APP_DB.connection.execute(
        "INSERT INTO cf_conversations (uid, id, created_at, source, status, visibility, folder_id, structured_json) "
        "VALUES (?, ?, ?, 'omi', 'completed', 'private', ?, ?)",
        ("folder-user", "conv-1", 200, source_folder["id"], json.dumps({"title": "Folder conversation"})),
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_conversations (uid, id, created_at, source, status, visibility, folder_id, is_locked) "
        "VALUES (?, ?, ?, 'omi', 'completed', 'private', ?, 1)",
        ("folder-user", "conv-locked", 100, source_folder["id"]),
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_conversations (uid, id, created_at, source, status, visibility, folder_id, discarded) "
        "VALUES (?, ?, ?, 'omi', 'completed', 'private', ?, 1)",
        ("folder-user", "conv-discarded", 50, source_folder["id"]),
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_conversations (uid, id, created_at, source, status, visibility, folder_id) "
        "VALUES (?, ?, ?, 'omi', 'completed', 'private', ?)",
        ("other-user", "conv-foreign", 300, source_folder["id"]),
    )
    env.APP_DB.connection.commit()

    listed = asyncio.run(list_folder_conversations(FakeRequest(env, headers), source_folder["id"]))
    assert [conversation["id"] for conversation in listed] == ["conv-1", "conv-locked"]
    listed_without_discarded = asyncio.run(
        list_folder_conversations(FakeRequest(env, headers, query={"include_discarded": "false"}), source_folder["id"])
    )
    assert [conversation["id"] for conversation in listed_without_discarded] == ["conv-1", "conv-locked"]
    invalid = asyncio.run(
        list_folder_conversations(FakeRequest(env, headers, query={"limit": "0"}), source_folder["id"])
    )
    assert invalid.status_code == 400

    moved = asyncio.run(
        move_conversation_to_folder(
            FakeRequest(env, headers, body={"folder_id": target_folder["id"]}),
            "conv-1",
        )
    )
    assert moved["status"] == "ok"
    assert moved["conversation"]["folder_id"] == target_folder["id"]
    assert asyncio.run(list_folder_conversations(FakeRequest(env, headers), target_folder["id"]))[0]["id"] == "conv-1"
    source_after = asyncio.run(get_folder(FakeRequest(env, headers), source_folder["id"]))
    target_after = asyncio.run(get_folder(FakeRequest(env, headers), target_folder["id"]))
    assert source_after["conversation_count"] == 1
    assert target_after["conversation_count"] == 1

    locked = asyncio.run(
        move_conversation_to_folder(
            FakeRequest(env, headers, body={"folder_id": target_folder["id"]}),
            "conv-locked",
        )
    )
    assert locked.status_code == 402
    missing_folder = asyncio.run(
        move_conversation_to_folder(FakeRequest(env, headers, body={"folder_id": "missing"}), "conv-1")
    )
    assert missing_folder.status_code == 404
