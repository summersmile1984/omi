import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from account_cutover_routes import get_account_cutover_control  # noqa: E402


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0034_account_cutover.sql").read_text())

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


class FakeRequest:
    def __init__(self, env, headers):
        self.scope = {"env": env}
        self.headers = headers


def signed_headers(secret: str, uid: str = "cutover-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "cutover-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def test_missing_cutover_row_projects_legacy_control():
    secret = "cutover-secret"
    db = FakeDb()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    control = asyncio.run(get_account_cutover_control(FakeRequest(env, signed_headers(secret))))
    assert control["state"] == "legacy"
    assert control["account_generation"] == 0
    assert control["client_action"] == "none"
    assert control["legacy_writes_allowed"] is True
    assert control["product_traffic_allowed"] is True
    assert len(control["minimum_supported_builds"]) == 6


def test_fenced_cutover_control_quarantines_and_hides_product_traffic():
    secret = "cutover-secret"
    db = FakeDb()
    db.connection.execute(
        "INSERT INTO cf_account_cutover "
        "(uid, state, account_generation, ui_generation, api_generation, stranded_new_data, "
        "offline_queue_instruction, checkpoint_phase, checkpoint_token, manifest_id, "
        "destination_backend_bound, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "cutover-user",
            "migrating",
            3,
            4,
            5,
            0,
            "none",
            "importing",
            "checkpoint-1",
            "manifest-1",
            1,
            10,
        ),
    )
    db.connection.commit()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    control = asyncio.run(
        get_account_cutover_control(
            FakeRequest(
                env,
                {
                    **signed_headers(secret),
                    "x-app-platform": "macos",
                    "x-app-version": "1.0+10",
                },
            )
        )
    )
    assert control["state"] == "migrating"
    assert control["account_generation"] == 3
    assert control["ui_generation"] == 4
    assert control["api_generation"] == 5
    assert control["client_action"] == "migration_maintenance"
    assert control["offline_queue_instruction"] == "quarantine"
    assert control["legacy_writes_allowed"] is False
    assert control["product_traffic_allowed"] is False
    assert control["migration"]["manifest_id"] == "manifest-1"


def test_malformed_cutover_row_fails_closed_and_auth_is_required():
    secret = "cutover-secret"
    db = FakeDb()
    db.connection.execute("PRAGMA ignore_check_constraints = ON")
    db.connection.execute(
        "INSERT INTO cf_account_cutover (uid, state, updated_at) VALUES (?, ?, ?)",
        ("cutover-user", "not-a-state", 10),
    )
    db.connection.commit()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()
    malformed = asyncio.run(get_account_cutover_control(FakeRequest(env, signed_headers(secret))))
    assert malformed.status_code == 503
    unauthorized = asyncio.run(get_account_cutover_control(FakeRequest(env, {})))
    assert unauthorized.status_code == 401
