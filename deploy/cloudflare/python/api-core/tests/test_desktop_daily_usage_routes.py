import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from desktop_daily_usage_routes import router  # noqa: E402


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        for path in sorted((Path(__file__).parents[3] / "migrations/app").glob("*.sql")):
            self.connection.executescript(path.read_text())

    def prepare(self, sql):
        connection = self.connection

        class Statement:
            def bind(self, *args):
                self.args = args
                return self

            async def run(self):
                cursor = connection.execute(sql, self.args)
                connection.commit()
                return {"success": True, "meta": {"changes": cursor.rowcount}}

        return Statement()


def headers(uid):
    encoded = base64.urlsafe_b64encode(json.dumps({"uid": uid}).encode()).decode().rstrip("=")
    signature = hmac.new(b"test-secret", encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


@pytest.fixture
def target():
    database = Database()
    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def environment(request, call_next):
        request.scope["env"] = SimpleNamespace(APP_DB=database, INTERNAL_ASSERTION_SECRET="test-secret")
        return await call_next(request)

    async def post(body, uid="owner"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://core.test") as client:
            return await client.post("/v1/users/desktop-usage/daily", json=body, headers=headers(uid) if uid else {})

    yield database.connection, lambda body, uid="owner": asyncio.run(post(body, uid))
    database.connection.close()


def payload(**changes):
    return {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "timezone": "UTC",
        "client_device_id": "desktop-a",
        "watching_seconds": 120,
        "listening_seconds": 80,
        "proactive_cards_shown": 3,
        "proactive_cards_acted": 1,
        "ptt_turns": 2,
        **changes,
    }


def test_monotonic_device_totals_are_isolated_by_account_and_day(target):
    db, post = target
    for data, uid in [
        (payload(), "owner"),
        (payload(watching_seconds=60, ptt_turns=4), "owner"),
        (payload(client_device_id="desktop-b", watching_seconds=30), "owner"),
        (payload(watching_seconds=20), "other"),
    ]:
        response = post(data, uid)
        assert response.status_code == 200 and response.json() == {"ok": True}
    rows = [dict(row) for row in db.execute("SELECT * FROM cf_desktop_daily_usage ORDER BY uid, client_device_id")]
    assert len(rows) == 3
    owner = [row for row in rows if row["uid"] == "owner"]
    assert owner[0]["watching_seconds"] == 120 and owner[0]["ptt_turns"] == 4
    assert sum(row["watching_seconds"] for row in owner) == 150
    assert rows[0]["uid"] == "other" and rows[0]["watching_seconds"] == 20
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    assert post(payload(date=yesterday)).status_code == 200
    assert db.execute("SELECT COUNT(*) FROM cf_desktop_daily_usage WHERE uid='owner'").fetchone()[0] == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"watching_seconds": True},
        {"watching_seconds": "1"},
        {"listening_seconds": 86401},
        {"ptt_turns": 10001},
        {"proactive_cards_acted": -1},
        {"client_device_id": " "},
        {"date": "2026-02-30"},
        {"date": "2026-1-1"},
        {"date": "2000-01-01"},
        {"timezone": "Not/AZone"},
    ],
)
def test_wire_validation_rejects_invalid_counters_dates_and_timezones(target, changes):
    db, post = target
    assert post(payload(**changes)).status_code == 422
    assert db.execute("SELECT COUNT(*) FROM cf_desktop_daily_usage").fetchone()[0] == 0


def test_legacy_native_account_needs_no_preexisting_usage_state_and_deletion_fences_writes(target):
    db, post = target
    assert post(payload(), None).status_code == 401
    assert post(payload()).status_code == 200
    db.execute(
        "INSERT INTO cf_account_deletion_tombstones (uid, completed_at, expires_at) VALUES ('owner', 1, 9999999999)"
    )
    response = post(payload(ptt_turns=9))
    assert response.status_code == 503
    assert db.execute("SELECT ptt_turns FROM cf_desktop_daily_usage").fetchone()[0] == 2
    assert post(payload(client_device_id="new-device")).status_code == 503
