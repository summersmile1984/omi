import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from limitless_import_routes import (  # noqa: E402
    cancel_import_job,
    delete_import_job,
    get_import_job_status,
    list_import_jobs,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for migration in sorted(migration_dir.glob("*.sql")):
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
        result = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return type("Result", (), {"meta": {"changes": result.rowcount}})()


class FakeRequest:
    def __init__(self, env, headers):
        self.scope = {"env": env}
        self.headers = headers


def signed_headers(secret: str, uid: str = "import-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "import-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(secret: str):
    database = FakeDb()
    database.connection.execute(
        "INSERT INTO cf_import_jobs "
        "(uid, job_id, source_type, source_object_key, source_filename, language_code, request_fingerprint, status, created_at, updated_at) "
        "VALUES (?, ?, 'limitless', ?, 'export.zip', 'en', ?, ?, 100, 100)",
        ("import-user", "job-1", "imports/import-user/job-1.zip", "a" * 64, "pending"),
    )
    database.connection.execute(
        "INSERT INTO cf_import_jobs "
        "(uid, job_id, source_type, source_object_key, source_filename, language_code, request_fingerprint, status, created_at, updated_at) "
        "VALUES (?, ?, 'limitless', ?, 'export.zip', 'en', ?, ?, 101, 101)",
        ("other-user", "job-2", "imports/other-user/job-2.zip", "b" * 64, "completed"),
    )
    database.connection.commit()
    deleted = []
    async def delete_object(_self, key):
        deleted.append(key)

    env = type(
        "Env",
        (),
        {
            "APP_DB": database,
            "ASSETS": type("Assets", (), {"delete": delete_object})(),
            "INTERNAL_ASSERTION_SECRET": secret,
        },
    )()
    return env, database, deleted


def test_import_lifecycle_is_authenticated_and_uid_scoped():
    secret = "limitless-import-secret"
    env, database, _deleted = make_env(secret)
    unauthorized = asyncio.run(get_import_job_status("job-1", FakeRequest(env, {})))
    assert unauthorized.status_code == 401

    stranger = asyncio.run(
        get_import_job_status("job-2", FakeRequest(env, signed_headers(secret, "import-user")))
    )
    assert stranger.status_code == 404

    jobs = asyncio.run(list_import_jobs(FakeRequest(env, signed_headers(secret)), limit=1))
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "job-1"
    database.connection.close()


def test_import_cancel_uses_cas_and_terminal_delete_cleans_r2():
    secret = "limitless-import-secret"
    env, database, deleted = make_env(secret)
    request = FakeRequest(env, signed_headers(secret))
    cancelled = asyncio.run(cancel_import_job("job-1", request))
    assert cancelled["status"] == "cancelled"
    repeated = asyncio.run(cancel_import_job("job-1", request))
    assert repeated.status_code == 409

    removed = asyncio.run(delete_import_job("job-1", request))
    assert removed == {"status": "ok", "job_id": "job-1"}
    assert deleted == ["imports/import-user/job-1.zip"]
    assert database.connection.execute(
        "SELECT COUNT(*) FROM cf_import_jobs WHERE uid = 'import-user'"
    ).fetchone()[0] == 0
    database.connection.close()
