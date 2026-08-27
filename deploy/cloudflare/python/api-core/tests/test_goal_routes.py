import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from goal_routes import (  # noqa: E402
    create_goal,
    delete_goal,
    get_current_goal,
    get_goal,
    list_goals,
    update_goal,
    update_goal_progress,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration = Path(__file__).parents[3] / "migrations/app/0018_goals.sql"
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


def signed_headers(secret: str, uid: str = "goal-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "goal-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def test_goal_metadata_and_progress_are_uid_scoped():
    secret = "goal-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)

    invalid = asyncio.run(create_goal(FakeRequest(env, headers, {"title": ""})))
    assert invalid.status_code == 400
    focused = asyncio.run(create_goal(FakeRequest(env, headers, {"title": "Focused now", "status": "focused"})))
    assert focused.status_code == 400

    created = asyncio.run(
        create_goal(
            FakeRequest(
                env,
                headers,
                {
                    "title": "Learn Japanese",
                    "desired_outcome": "Hold a basic conversation",
                    "success_criteria": ["Finish N5", "Practice weekly"],
                    "goal_type": "numeric",
                    "target_value": 100,
                    "current_value": 10,
                    "unit": "sessions",
                },
            )
        )
    )
    assert created["id"].startswith("goal_")
    assert created["goal_id"] == created["id"]
    assert created["status"] == "background"
    assert created["metric"]["current"] == 10
    assert created["target_value"] == 100

    assert asyncio.run(get_current_goal(FakeRequest(env, headers)))["id"] == created["id"]
    assert len(asyncio.run(list_goals(FakeRequest(env, headers)))) == 1

    updated = asyncio.run(
        update_goal(FakeRequest(env, headers, {"why_it_matters": "Travel with confidence"}), created["id"])
    )
    assert updated["why_it_matters"] == "Travel with confidence"

    progress = asyncio.run(
        update_goal_progress(FakeRequest(env, headers, query={"current_value": "25"}), created["id"])
    )
    assert progress["metric"]["current"] == 25
    assert progress["current_value"] == 25

    other = asyncio.run(get_goal(FakeRequest(env, signed_headers(secret, "other-user")), created["id"]))
    assert other.status_code == 404

    deleted = asyncio.run(delete_goal(FakeRequest(env, headers), created["id"]))
    assert deleted == {"success": True, "deleted_id": created["id"]}
    assert asyncio.run(get_current_goal(FakeRequest(env, headers))) is None
    ended = asyncio.run(list_goals(FakeRequest(env, headers, query={"include_ended": "true"})))
    assert ended[0]["status"] == "abandoned"
    assert ended[0]["is_active"] is False


def test_goal_routes_reject_invalid_progress_and_empty_update():
    secret = "goal-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    created = asyncio.run(create_goal(FakeRequest(env, headers, {"title": "Read more"})))

    invalid_progress = asyncio.run(update_goal_progress(FakeRequest(env, headers), created["id"]))
    assert invalid_progress.status_code == 400
    invalid_update = asyncio.run(update_goal(FakeRequest(env, headers, {}), created["id"]))
    assert invalid_update.status_code == 400
