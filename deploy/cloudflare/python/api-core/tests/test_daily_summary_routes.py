import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from daily_summary_routes import (  # noqa: E402
    delete_daily_summary,
    get_daily_summary,
    list_daily_summaries,
    set_daily_summary_visibility,
    test_daily_summary as generate_daily_summary,
)


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
        self.connection.executescript((migration_dir / "0032_conversations.sql").read_text())
        self.connection.executescript((migration_dir / "0041_daily_summaries.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeRequest:
    def __init__(self, env, headers, query=None, body=None):
        self.scope = {"env": env}
        self.headers = headers
        self.query_params = query or {}
        self._body = body or {}

    async def json(self):
        return self._body


def signed_headers(secret: str, uid: str = "summary-user"):
    raw = json.dumps({"uid": uid}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def insert_summary(db: FakeDb, uid: str, summary_id: str, date_text: str):
    now = 1_700_000_000
    db.connection.execute(
        "INSERT INTO cf_daily_summaries "
        "(uid, id, date, headline, day_emoji, overview, stats_json, highlights_json, action_items_json, "
        "unresolved_questions_json, decisions_made_json, knowledge_nuggets_json, locations_json, visibility, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uid,
            summary_id,
            date_text,
            "A day",
            "📅",
            "Overview",
            json.dumps({"total_conversations": 1, "total_duration_minutes": 2, "action_items_count": 0}),
            "[]",
            "[]",
            "[]",
            "[]",
            "[]",
            "[]",
            "private",
            now,
            now,
        ),
    )
    db.connection.commit()


def test_daily_summary_projection_is_uid_scoped_and_returns_wrapped_list():
    secret = "summary-secret"
    db = FakeDb()
    insert_summary(db, "summary-user", "summary-1", "2026-08-27")
    insert_summary(db, "other-user", "summary-2", "2026-08-28")
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    result = asyncio.run(list_daily_summaries(FakeRequest(env, signed_headers(secret))))
    assert [item["id"] for item in result["summaries"]] == ["summary-1"]
    assert result["summaries"][0]["stats"]["total_conversations"] == 1


def test_daily_summary_mutations_validate_owner_and_visibility():
    secret = "summary-secret"
    db = FakeDb()
    insert_summary(db, "summary-user", "summary-1", "2026-08-27")
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    detail = asyncio.run(get_daily_summary(FakeRequest(env, signed_headers(secret)), "summary-1"))
    assert detail["date"] == "2026-08-27"
    changed = asyncio.run(
        set_daily_summary_visibility(FakeRequest(env, signed_headers(secret), {"value": "shared"}), "summary-1")
    )
    assert changed == {"status": "Ok"}
    invalid = asyncio.run(
        set_daily_summary_visibility(FakeRequest(env, signed_headers(secret), {"value": "public"}), "summary-1")
    )
    assert invalid.status_code == 400
    missing = asyncio.run(get_daily_summary(FakeRequest(env, signed_headers(secret)), "missing"))
    assert missing.status_code == 404
    deleted = asyncio.run(delete_daily_summary(FakeRequest(env, signed_headers(secret)), "summary-1"))
    assert deleted == {"status": "ok"}


def test_daily_summary_test_generation_uses_d1_conversations_without_llm():
    secret = "summary-secret"
    db = FakeDb()
    db.connection.execute(
        "INSERT INTO cf_conversations "
        "(uid, id, created_at, started_at, finished_at, structured_json) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "summary-user",
            "conversation-1",
            1_756_512_000,
            1_756_512_000,
            1_756_512_120,
            json.dumps({"action_items": [{"description": "ship"}]}),
        ),
    )
    db.connection.commit()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    result = asyncio.run(generate_daily_summary(FakeRequest(env, signed_headers(secret), body={"date": "2025-08-30"})))
    assert result["status"] == "ok"
    assert result["conversations_count"] == 1
    stored = db.connection.execute("SELECT stats_json FROM cf_daily_summaries").fetchone()
    assert json.loads(stored[0])["action_items_count"] == 1
