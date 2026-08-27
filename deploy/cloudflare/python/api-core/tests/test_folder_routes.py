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
    list_folders,
    reorder_folders,
    update_folder,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration = Path(__file__).parents[3] / "migrations/app/0019_folders.sql"
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
