import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from advice_routes import (  # noqa: E402
    create_advice,
    delete_advice,
    list_advice,
    mark_all_advice_read,
    update_advice,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            "CREATE TABLE cf_account_deletion_intents (uid TEXT PRIMARY KEY);"
            "CREATE TABLE cf_account_deletion_tombstones (uid TEXT PRIMARY KEY, expires_at INTEGER NOT NULL);"
        )
        migration = Path(__file__).parents[3] / "migrations/app/0071_advice.sql"
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
        self.connection.commit()
        return dict(row) if row is not None else None

    async def all(self):
        rows = self.connection.execute(self.sql, self.args).fetchall()
        self.connection.commit()
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


def signed_headers(secret: str, uid: str = "advice-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "advice-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(secret: str):
    return type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()


def create(env, secret: str, *, uid: str = "advice-user", **body):
    payload = {"content": body.pop("content", "Take a short break"), **body}
    return asyncio.run(create_advice(FakeRequest(env, signed_headers(secret, uid), body=payload)))


def test_advice_crud_filters_and_mark_all_are_uid_scoped():
    secret = "advice-secret"
    env = make_env(secret)
    focus = create(
        env,
        secret,
        content="Return to the current task",
        category="focus",
        reasoning="Several context switches",
        confidence=0.8,
    )
    dismissed = create(env, secret, content="Drink water", category="wellness")
    create(env, secret, uid="other-user", content="Other account")

    assert focus["category"] == "focus"
    assert focus["confidence"] == 0.8
    assert focus["is_read"] is False
    assert focus["is_dismissed"] is False
    assert focus["created_at"].endswith("+00:00")

    dismissed = asyncio.run(
        update_advice(
            FakeRequest(env, signed_headers(secret), body={"is_dismissed": True}),
            dismissed["id"],
        )
    )
    assert dismissed["is_read"] is False
    assert dismissed["is_dismissed"] is True
    focus = asyncio.run(
        update_advice(
            FakeRequest(env, signed_headers(secret), body={"is_read": True}),
            focus["id"],
        )
    )
    assert focus["is_read"] is True
    assert focus["is_dismissed"] is False

    visible = asyncio.run(list_advice(FakeRequest(env, signed_headers(secret))))
    assert [item["id"] for item in visible] == [focus["id"]]
    by_category = asyncio.run(list_advice(FakeRequest(env, signed_headers(secret), query={"category": "focus"})))
    assert [item["id"] for item in by_category] == [focus["id"]]
    all_items = asyncio.run(list_advice(FakeRequest(env, signed_headers(secret), query={"include_dismissed": "yes"})))
    assert {item["id"] for item in all_items} == {focus["id"], dismissed["id"]}

    result = asyncio.run(mark_all_advice_read(FakeRequest(env, signed_headers(secret))))
    assert result == {"status": "marked 1 as read"}
    all_items = asyncio.run(list_advice(FakeRequest(env, signed_headers(secret), query={"include_dismissed": "true"})))
    assert all(item["is_read"] for item in all_items)

    assert asyncio.run(delete_advice(FakeRequest(env, signed_headers(secret, "other-user")), focus["id"])) == {
        "status": "ok"
    }
    assert len(asyncio.run(list_advice(FakeRequest(env, signed_headers(secret))))) == 1
    assert asyncio.run(delete_advice(FakeRequest(env, signed_headers(secret)), focus["id"])) == {"status": "ok"}
    assert asyncio.run(delete_advice(FakeRequest(env, signed_headers(secret)), focus["id"])) == {"status": "ok"}
    assert asyncio.run(list_advice(FakeRequest(env, signed_headers(secret)))) == []


def test_advice_routes_reject_invalid_inputs_missing_rows_and_deletion_fences():
    secret = "advice-secret"
    env = make_env(secret)

    assert asyncio.run(list_advice(FakeRequest(env, {}))).status_code == 401
    assert asyncio.run(create_advice(FakeRequest(env, signed_headers(secret), body={"content": ""}))).status_code == 422
    assert (
        asyncio.run(
            create_advice(FakeRequest(env, signed_headers(secret), body={"content": "Advice", "confidence": 2}))
        ).status_code
        == 422
    )
    assert (
        asyncio.run(list_advice(FakeRequest(env, signed_headers(secret), query={"limit": "1001"}))).status_code == 422
    )
    assert (
        asyncio.run(
            list_advice(FakeRequest(env, signed_headers(secret), query={"include_dismissed": "sometimes"}))
        ).status_code
        == 422
    )
    missing = asyncio.run(update_advice(FakeRequest(env, signed_headers(secret), body={"is_read": True}), "missing"))
    assert missing.status_code == 404

    env.APP_DB.connection.execute("INSERT INTO cf_account_deletion_intents (uid) VALUES (?)", ("advice-user",))
    env.APP_DB.connection.commit()
    fenced = create(env, secret)
    assert fenced.status_code == 503
