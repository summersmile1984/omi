import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mcp_app_projection_routes import get_mcp_app_tools  # noqa: E402


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
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            "CREATE TABLE cf_account_deletion_intents (uid TEXT PRIMARY KEY);"
            "CREATE TABLE cf_account_deletion_tombstones (uid TEXT PRIMARY KEY);"
        )
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0035_app_catalog.sql").read_text())
        self.connection.executescript(
            "ALTER TABLE cf_app_catalog ADD COLUMN owner_uid TEXT;"
            "CREATE TABLE cf_user_enabled_apps ("
            "uid TEXT NOT NULL, app_id TEXT NOT NULL, created_at INTEGER NOT NULL,"
            "PRIMARY KEY (uid, app_id));"
        )
        self.connection.executescript((migration_dir / "0112_mcp_app_authority.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeRequest:
    def __init__(self, env, headers=None, query=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.query_params = query or {}


def signed_headers(secret: str, uid: str = "mcp-user"):
    raw = json.dumps({"uid": uid}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def add_app(db, app_id: str, *, uid: str = "mcp-user", discovery_status: str = "ready", tools=None):
    tools = tools or [{"name": "lookup", "description": "Look up a value", "inputSchema": {"type": "object"}}]
    db.connection.execute(
        "INSERT INTO cf_app_catalog (id, owner_uid, approved, disabled, data_json, updated_at) "
        "VALUES (?, ?, 0, 0, ?, 1)",
        (app_id, uid, json.dumps({"id": app_id, "name": "MCP app", "description": "Remote tools"})),
    )
    db.connection.execute(
        "INSERT INTO cf_user_enabled_apps (uid, app_id, created_at) VALUES (?, ?, 1)",
        (uid, app_id),
    )
    db.connection.execute(
        "INSERT INTO cf_mcp_app_connections "
        "(app_id, owner_uid, server_url, status, created_at, updated_at) "
        "VALUES (?, ?, 'https://provider.example/mcp', 'authorized', 1, 1)",
        (app_id, uid),
    )
    db.connection.execute(
        "INSERT INTO cf_mcp_app_discoveries "
        "(app_id, owner_uid, endpoint, protocol_version, tools_json, status, revision, fetched_at, updated_at) "
        "VALUES (?, ?, 'https://provider.example/mcp', '2025-03-26', ?, ?, 3, 1, 1)",
        (app_id, uid, json.dumps(tools), discovery_status),
    )
    db.connection.commit()


def response_json(response):
    return json.loads(response.body)


def test_projection_is_installed_owner_scoped_and_does_not_expose_provider_fields():
    secret = "mcp-projection-secret"
    db = FakeDb()
    add_app(db, "ready-app")
    add_app(db, "other-owner", uid="other-user")
    add_app(db, "failed-app", discovery_status="failed")
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    response = asyncio.run(get_mcp_app_tools(FakeRequest(env, signed_headers(secret))))

    assert response.status_code == 200
    payload = response_json(response)
    assert payload["count"] == 1
    assert payload["apps"][0] == {
        "app_id": "ready-app",
        "name": "MCP app",
        "description": "Remote tools",
        "protocol_version": "2025-03-26",
        "revision": 3,
        "tools": [{"name": "lookup", "description": "Look up a value", "inputSchema": {"type": "object"}}],
    }
    assert "provider.example" not in json.dumps(payload)
    assert response.headers["cache-control"] == "no-store"


def test_projection_supports_one_app_and_rejects_invalid_or_unauthenticated_reads():
    secret = "mcp-projection-secret"
    db = FakeDb()
    add_app(db, "ready-app")
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    unauthorized = asyncio.run(get_mcp_app_tools(FakeRequest(env)))
    assert unauthorized.status_code == 401
    invalid = asyncio.run(
        get_mcp_app_tools(FakeRequest(env, signed_headers(secret), {"app_id": ""}))
    )
    assert invalid.status_code == 400
    missing = asyncio.run(
        get_mcp_app_tools(FakeRequest(env, signed_headers(secret), {"app_id": "missing"}))
    )
    assert response_json(missing) == {"apps": [], "count": 0}


def test_projection_fails_closed_on_malformed_stored_tools():
    secret = "mcp-projection-secret"
    db = FakeDb()
    add_app(db, "malformed", tools=[{"name": "ok"}])
    db.connection.execute(
        "UPDATE cf_mcp_app_discoveries SET tools_json = ? WHERE app_id = 'malformed'", (json.dumps([{"name": "bad\u0000"}]),)
    )
    db.connection.commit()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    response = asyncio.run(get_mcp_app_tools(FakeRequest(env, signed_headers(secret))))

    assert response.status_code == 503
    assert response_json(response) == {"error": "mcp tools unavailable"}
