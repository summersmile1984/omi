import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from focus_routes import (  # noqa: E402
    create_focus_session,
    delete_focus_session,
    get_focus_stats,
    list_focus_sessions,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0020_focus_sessions.sql").read_text())

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


def signed_headers(secret: str, uid: str = "focus-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "focus-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def epoch(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


def test_focus_session_crud_and_daily_stats_are_uid_scoped():
    secret = "focus-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)

    created = asyncio.run(
        create_focus_session(
            FakeRequest(
                env,
                headers,
                {
                    "status": "distracted",
                    "app_or_site": "Chat",
                    "description": "Context switch",
                    "message": "Later",
                    "duration_seconds": 90,
                },
            )
        )
    )
    assert created["status"] == "distracted"
    assert created["duration_seconds"] == 90

    env.APP_DB.connection.execute(
        "INSERT INTO cf_focus_sessions "
        "(uid, id, status, app_or_site, description, message, created_at, duration_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "focus-user",
            "focused-1",
            "focused",
            "Editor",
            "Deep work",
            None,
            epoch("2026-08-28T09:00:00"),
            125,
        ),
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_focus_sessions "
        "(uid, id, status, app_or_site, description, message, created_at, duration_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "focus-user",
            "distracted-2",
            "distracted",
            "Chat",
            "Another switch",
            None,
            epoch("2026-08-28T10:00:00"),
            None,
        ),
    )
    env.APP_DB.connection.commit()
    env.APP_DB.connection.execute(
        "UPDATE cf_focus_sessions SET created_at = ? WHERE uid = ? AND id = ?",
        (epoch("2026-08-28T11:00:00"), "focus-user", created["id"]),
    )
    env.APP_DB.connection.commit()

    rows = asyncio.run(list_focus_sessions(FakeRequest(env, headers, query={"date": "2026-08-28"})))
    assert len(rows) == 3
    assert rows[0]["id"] == created["id"]
    stats = asyncio.run(get_focus_stats(FakeRequest(env, headers, query={"date": "2026-08-28"})))
    assert stats["session_count"] == 3
    assert stats["focused_count"] == 1
    assert stats["distracted_count"] == 2
    assert stats["focused_minutes"] == 2
    assert stats["distracted_minutes"] == 2
    assert stats["top_distractions"] == [{"app_or_site": "Chat", "total_seconds": 150, "count": 2}]

    assert asyncio.run(delete_focus_session(FakeRequest(env, headers), created["id"])) == {"status": "ok"}
    assert len(asyncio.run(list_focus_sessions(FakeRequest(env, headers, query={"date": "2026-08-28"})))) == 2
    assert len(asyncio.run(list_focus_sessions(FakeRequest(env, signed_headers(secret, "other-user"))))) == 0


def test_focus_routes_reject_invalid_inputs_and_auth():
    secret = "focus-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    invalid = asyncio.run(
        create_focus_session(FakeRequest(env, signed_headers(secret), {"status": "unknown", "app_or_site": "x"}))
    )
    assert invalid.status_code == 400
    bad_date = asyncio.run(list_focus_sessions(FakeRequest(env, signed_headers(secret), query={"date": "2026-02-30"})))
    assert bad_date.status_code == 422
    unauthenticated = asyncio.run(get_focus_stats(FakeRequest(env, {})))
    assert unauthenticated.status_code == 401
