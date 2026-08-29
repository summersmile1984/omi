import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from app_install_routes import disable_app, enable_app, get_enabled_apps  # noqa: E402


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


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0035_app_catalog.sql").read_text())
        self.connection.executescript((migration_dir / "0036_app_installations.sql").read_text())
        self.connection.executescript(
            "CREATE TABLE cf_account_deletion_intents (uid TEXT PRIMARY KEY);"
            "CREATE TABLE cf_account_deletion_tombstones (uid TEXT PRIMARY KEY);"
        )
        self.connection.executescript((migration_dir / "0060_app_subscriptions.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeRequest:
    def __init__(self, env, headers=None, query=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.query_params = query or {}


def signed_headers(secret: str, uid: str = "install-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "install-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def catalog_row(connection, app_id: str, payload: dict[str, object], *, installs: int = 0):
    connection.execute(
        "INSERT INTO cf_app_catalog (id, approved, disabled, installs, data_json, updated_at) VALUES (?, 1, 0, ?, ?, 1)",
        (app_id, installs, json.dumps(payload)),
    )
    connection.commit()


def test_free_public_app_install_is_idempotent_and_updates_counter():
    secret = "install-secret"
    db = FakeDb()
    catalog_row(db.connection, "free-app", {"id": "free-app", "capabilities": ["chat"], "is_paid": False})
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()
    request = FakeRequest(env, signed_headers(secret), {"app_id": "free-app"})

    assert asyncio.run(enable_app(request)) == {"status": "ok"}
    assert asyncio.run(enable_app(request)) == {"status": "ok"}
    enabled = asyncio.run(get_enabled_apps(FakeRequest(env, signed_headers(secret))))
    assert enabled == ["free-app"]
    assert db.connection.execute("SELECT installs FROM cf_app_catalog WHERE id = 'free-app'").fetchone()[0] == 1

    assert asyncio.run(disable_app(request)) == {"status": "ok"}
    missing = asyncio.run(disable_app(request))
    assert missing.status_code == 404
    assert db.connection.execute("SELECT installs FROM cf_app_catalog WHERE id = 'free-app'").fetchone()[0] == 0


def test_install_route_rejects_paid_setup_and_unknown_apps_and_requires_auth():
    secret = "install-secret"
    db = FakeDb()
    catalog_row(db.connection, "paid-app", {"id": "paid-app", "capabilities": ["chat"], "is_paid": True})
    catalog_row(
        db.connection,
        "setup-app",
        {
            "id": "setup-app",
            "capabilities": ["external_integration"],
            "external_integration": {"setup_completed_url": "https://example.test/setup"},
        },
    )
    catalog_row(db.connection, "mismatch-app", {"id": "different-id", "capabilities": ["chat"]})
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    assert asyncio.run(get_enabled_apps(FakeRequest(env))).status_code == 401
    paid = asyncio.run(enable_app(FakeRequest(env, signed_headers(secret), {"app_id": "paid-app"})))
    assert paid.status_code == 403
    db.connection.execute(
        "INSERT INTO cf_app_subscriptions "
        "(uid, app_id, stripe_customer_id, stripe_subscription_id, status, current_period_end, "
        "price_id, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?, 1, 1)",
        (
            "install-user",
            "paid-app",
            "cus_installUser123",
            "sub_installUser123",
            4_000_000_000,
            "price_installUser123",
        ),
    )
    db.connection.commit()
    assert asyncio.run(enable_app(FakeRequest(env, signed_headers(secret), {"app_id": "paid-app"}))) == {"status": "ok"}
    assert asyncio.run(get_enabled_apps(FakeRequest(env, signed_headers(secret)))) == ["paid-app"]
    db.connection.execute(
        "UPDATE cf_app_subscriptions SET current_period_end = 1 WHERE uid = ? AND app_id = ?",
        ("install-user", "paid-app"),
    )
    db.connection.commit()
    assert asyncio.run(get_enabled_apps(FakeRequest(env, signed_headers(secret)))) == []
    setup = asyncio.run(enable_app(FakeRequest(env, signed_headers(secret), {"app_id": "setup-app"})))
    assert setup.status_code == 400
    unknown = asyncio.run(enable_app(FakeRequest(env, signed_headers(secret), {"app_id": "missing"})))
    assert unknown.status_code == 404
    mismatch = asyncio.run(enable_app(FakeRequest(env, signed_headers(secret), {"app_id": "mismatch-app"})))
    assert mismatch.status_code == 503


def test_install_route_validates_app_id():
    secret = "install-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    invalid = asyncio.run(enable_app(FakeRequest(env, signed_headers(secret), {"app_id": ""})))
    assert invalid.status_code == 400
