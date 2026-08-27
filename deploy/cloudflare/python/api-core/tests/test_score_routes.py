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

from score_routes import get_daily_score, get_scores  # noqa: E402


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0016_action_items.sql").read_text())

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
    def __init__(self, env, headers, query=None):
        self.scope = {"env": env}
        self.headers = headers
        self.query_params = query or {}


def signed_headers(secret: str, uid: str = "score-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "score-test"},
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


def insert_task(env, *, uid, task_id, created_at, due_at, completed, deleted=0):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_action_items "
        "(uid, id, description, status, completed, due_at, created_at, updated_at, deleted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uid,
            task_id,
            task_id,
            "completed" if completed else "active",
            completed,
            due_at,
            created_at,
            created_at,
            deleted,
        ),
    )
    env.APP_DB.connection.commit()


def test_scores_use_d1_action_items_and_ignore_deleted_rows():
    secret = "score-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    uid = "score-user"
    day = "2026-08-28"
    insert_task(
        env,
        uid=uid,
        task_id="daily-complete",
        created_at=epoch("2026-08-27T12:00:00"),
        due_at=epoch("2026-08-28T09:00:00"),
        completed=1,
    )
    insert_task(
        env,
        uid=uid,
        task_id="daily-active",
        created_at=epoch("2026-08-28T10:00:00"),
        due_at=epoch("2026-08-28T11:00:00"),
        completed=0,
    )
    insert_task(
        env,
        uid=uid,
        task_id="weekly-active",
        created_at=epoch("2026-08-22T12:00:00"),
        due_at=epoch("2026-08-30T12:00:00"),
        completed=0,
    )
    insert_task(
        env,
        uid=uid,
        task_id="deleted-complete",
        created_at=epoch("2026-08-28T12:00:00"),
        due_at=epoch("2026-08-28T12:00:00"),
        completed=1,
        deleted=1,
    )

    headers = signed_headers(secret, uid)
    daily = asyncio.run(get_daily_score(FakeRequest(env, headers, {"date": day})))
    assert daily == {"date": day, "score": 50, "completed_tasks": 1, "total_tasks": 2}

    scores = asyncio.run(get_scores(FakeRequest(env, headers, {"date": day})))
    assert scores["daily"] == {"score": 50.0, "completed_tasks": 1, "total_tasks": 2}
    assert scores["weekly"] == {"score": 33.3, "completed_tasks": 1, "total_tasks": 3}
    assert scores["overall"] == {"score": 33.3, "completed_tasks": 1, "total_tasks": 3}
    assert scores["default_tab"] == "daily"


def test_scores_reject_bad_dates_and_missing_auth():
    secret = "score-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    invalid = asyncio.run(get_daily_score(FakeRequest(env, signed_headers(secret), {"date": "2026-02-30"})))
    assert invalid.status_code == 422
    unauthenticated = asyncio.run(get_scores(FakeRequest(env, {})))
    assert unauthenticated.status_code == 401
