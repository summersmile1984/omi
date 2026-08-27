import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from people_routes import (  # noqa: E402
    delete_person,
    get_or_create_person,
    get_person,
    list_people,
    rename_person,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration = Path(__file__).parents[3] / "migrations/app/0017_people.sql"
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


def signed_headers(secret: str, uid: str = "people-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "people-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def test_people_metadata_is_uid_scoped_and_name_idempotent():
    secret = "people-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)

    invalid = asyncio.run(get_or_create_person(FakeRequest(env, headers, {"name": "A"})))
    assert invalid.status_code == 400

    created = asyncio.run(get_or_create_person(FakeRequest(env, headers, {"name": "Alice"})))
    assert created["id"]
    assert created["name"] == "Alice"
    assert created["speech_samples"] == []
    assert created["speech_samples_version"] == 3

    retry = asyncio.run(get_or_create_person(FakeRequest(env, headers, {"name": "Alice"})))
    assert retry["id"] == created["id"]

    listed = asyncio.run(list_people(FakeRequest(env, headers)))
    assert listed == [created]
    without_samples = asyncio.run(list_people(FakeRequest(env, headers, query={"include_speech_samples": "false"})))
    assert without_samples[0]["speech_samples"] == []

    renamed = asyncio.run(rename_person(FakeRequest(env, headers, query={"value": "Alice Chen"}), created["id"]))
    assert renamed == {"status": "ok"}
    fetched = asyncio.run(get_person(FakeRequest(env, headers), created["id"]))
    assert fetched["name"] == "Alice Chen"

    other = asyncio.run(get_person(FakeRequest(env, signed_headers(secret, "other-user")), created["id"]))
    assert other.status_code == 404

    deleted = asyncio.run(delete_person(FakeRequest(env, headers), created["id"]))
    assert deleted.status_code == 204
    missing = asyncio.run(get_person(FakeRequest(env, headers), created["id"]))
    assert missing.status_code == 404


def test_people_routes_reject_invalid_boolean_and_duplicate_rename():
    secret = "people-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    first = asyncio.run(get_or_create_person(FakeRequest(env, headers, {"name": "Alice"})))
    second = asyncio.run(get_or_create_person(FakeRequest(env, headers, {"name": "Bob"})))

    invalid_filter = asyncio.run(list_people(FakeRequest(env, headers, query={"include_speech_samples": "maybe"})))
    assert invalid_filter.status_code == 400

    duplicate = asyncio.run(rename_person(FakeRequest(env, headers, query={"value": "Bob"}), first["id"]))
    assert duplicate.status_code == 409

    not_found = asyncio.run(rename_person(FakeRequest(env, headers, query={"value": "Carol"}), "missing-person"))
    assert not_found.status_code == 404
    assert second["name"] == "Bob"
