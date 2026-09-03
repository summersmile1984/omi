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

from calendar_meeting_routes import (  # noqa: E402
    get_calendar_meeting,
    list_calendar_meetings,
    store_calendar_meeting,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration = Path(__file__).parents[3] / "migrations/app/0030_calendar_meetings.sql"
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
    def __init__(self, env, headers, body=None):
        self.scope = {"env": env}
        self.headers = headers
        self.body = body

    async def json(self):
        return self.body


def signed_headers(secret: str, uid: str = "calendar-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "calendar-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def _body(title="Weekly sync", *, start="2026-08-28T09:00:00+00:00"):
    return {
        "calendar_event_id": "event-1",
        "calendar_source": "macos_calendar",
        "title": title,
        "start_time": start,
        "end_time": "2026-08-28T10:30:00+00:00",
        "platform": "Zoom",
        "meeting_link": "https://zoom.example.test/meeting",
        "participants": [{"name": "Ada", "email": "ada@example.test"}],
        "notes": "agenda",
    }


def test_calendar_meetings_upsert_natural_key_and_bound_uid_reads():
    secret = "calendar-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)

    created = asyncio.run(store_calendar_meeting(FakeRequest(env, headers, _body())))
    updated = asyncio.run(
        store_calendar_meeting(
            FakeRequest(
                env,
                headers,
                {
                    **_body("Weekly sync updated"),
                    "start_time": "2026-08-28T11:00:00+00:00",
                    "end_time": "2026-08-28T12:30:00+00:00",
                },
            )
        )
    )
    assert updated["meeting_id"] == created["meeting_id"]

    fetched = asyncio.run(get_calendar_meeting(FakeRequest(env, headers), created["meeting_id"]))
    assert fetched["title"] == "Weekly sync updated"
    assert fetched["duration_minutes"] == 90
    assert fetched["participants"] == [{"name": "Ada", "email": "ada@example.test"}]
    assert fetched["start_time"] == "2026-08-28T11:00:00+00:00"

    listed = asyncio.run(list_calendar_meetings(FakeRequest(env, headers), limit=50))
    assert len(listed) == 1
    filtered = asyncio.run(
        list_calendar_meetings(
            FakeRequest(env, headers),
            start_date=datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
            limit=50,
        )
    )
    assert filtered == []
    other = asyncio.run(get_calendar_meeting(FakeRequest(env, signed_headers(secret, "other-user")), created["meeting_id"]))
    assert other.status_code == 404


def test_calendar_meetings_reject_invalid_input_and_unsigned_requests():
    secret = "calendar-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    unauthorized = asyncio.run(store_calendar_meeting(FakeRequest(env, {}, _body())))
    assert unauthorized.status_code == 401

    invalid_window = asyncio.run(
        store_calendar_meeting(
            FakeRequest(env, signed_headers(secret), {**_body(), "end_time": "2026-08-28T08:00:00+00:00"})
        )
    )
    assert invalid_window.status_code == 400

    invalid_limit = asyncio.run(list_calendar_meetings(FakeRequest(env, signed_headers(secret)), limit=51))
    assert invalid_limit.status_code == 422
