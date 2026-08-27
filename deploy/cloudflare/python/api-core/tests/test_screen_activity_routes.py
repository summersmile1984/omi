import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from screen_activity_routes import list_screen_activity, screen_activity_summary, sync_screen_activity  # noqa: E402


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0021_screen_activity.sql").read_text())

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
        self._body = body
        self.query_params = query or {}

    async def body(self):
        return json.dumps(self._body).encode()


def signed_headers(secret: str, uid: str = "screen-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "screen-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def test_screen_activity_sync_upserts_text_and_serves_summary():
    secret = "screen-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    body = {
        "rows": [
            {
                "id": 2,
                "timestamp": "2026-08-28T10:00:00Z",
                "appName": "Editor",
                "windowTitle": "Notes",
                "ocrText": "draft",
                "clientDeviceId": "mac-1",
                "embedding": [0.1, 0.2],
            },
            {
                "id": 1,
                "timestamp": "2026-08-28T09:00:00.123Z",
                "appName": "Browser",
                "windowTitle": "Docs",
                "ocrText": "read",
            },
        ]
    }
    synced = asyncio.run(sync_screen_activity(FakeRequest(env, headers, body)))
    assert synced["synced"] == 2
    assert synced["last_id"] == 2

    updated = asyncio.run(
        sync_screen_activity(
            FakeRequest(
                env,
                headers,
                {
                    "rows": [
                        {
                            "id": 2,
                            "timestamp": "2026-08-28T10:00:00Z",
                            "appName": "Editor",
                            "windowTitle": "Updated",
                            "ocrText": "changed",
                            "clientDeviceId": "mac-1",
                        }
                    ]
                },
            )
        )
    )
    assert updated["synced"] == 1
    rows = asyncio.run(list_screen_activity(FakeRequest(env, headers, query={"date": "2026-08-28"})))
    assert [row["id"] for row in rows] == ["1", "mac-1-2"]
    assert rows[1]["windowTitle"] == "Updated"
    assert "embedding" not in rows[1]

    summary = asyncio.run(screen_activity_summary(FakeRequest(env, headers, query={"date": "2026-08-28"})))
    assert summary["total_screenshots"] == 2
    assert summary["apps"]["Editor"] == {
        "count": 1,
        "first_seen": "2026-08-28 10:00:00.000",
        "last_seen": "2026-08-28 10:00:00.000",
        "window_titles": ["Updated"],
    }
    other = asyncio.run(list_screen_activity(FakeRequest(env, signed_headers(secret, "other-user"))))
    assert other == []


def test_screen_activity_rejects_bad_dates_batches_and_auth():
    secret = "screen-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    bad_date = asyncio.run(list_screen_activity(FakeRequest(env, signed_headers(secret), query={"date": "2026-02-30"})))
    assert bad_date.status_code == 422
    too_many = asyncio.run(
        sync_screen_activity(
            FakeRequest(
                env,
                signed_headers(secret),
                {"rows": [{"id": i, "timestamp": "2026-08-28T00:00:00Z"} for i in range(101)]},
            )
        )
    )
    assert too_many.status_code == 400
    unauthenticated = asyncio.run(screen_activity_summary(FakeRequest(env, {})))
    assert unauthenticated.status_code == 401
