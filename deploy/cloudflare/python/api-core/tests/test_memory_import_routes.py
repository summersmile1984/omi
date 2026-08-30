import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from memory_import_routes import create_memory_import_batch  # noqa: E402


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for migration in sorted(migration_dir.glob("*.sql")):
            self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        try:
            for statement in statements:
                self.connection.execute(statement.sql, statement.args)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


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


class FakeRequest:
    def __init__(self, env, headers, body):
        self.scope = {"env": env}
        self.headers = headers
        self._body = json.dumps(body).encode()

    async def body(self):
        return self._body


def signed_headers(secret: str, uid: str = "import-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "memory-import-test"},
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


def test_memory_import_batch_is_authenticated_and_deduplicates_artifacts():
    secret = "memory-import-secret"
    env = make_env(secret)
    request = {
        "source_type": " Pocket-Notes ",
        "import_run_id": "client-run-1",
        "source_account_hash": "account-hash",
        "importer_version": "v2",
        "items": [
            {
                "external_id": "note-1",
                "title": "First",
                "content": "A remembered note",
                "metadata": {"source": "pocket"},
            },
            {
                "external_id": "note-1",
                "title": "First",
                "content": "A remembered note",
                "metadata": {"source": "pocket"},
            },
            {"title": "Second", "snippet": "A short excerpt"},
        ],
    }

    response = asyncio.run(create_memory_import_batch(FakeRequest(env, signed_headers(secret), request)))
    assert response == {
        "run_id": "client-run-1",
        "artifacts_received": 3,
        "artifacts_created": 2,
        "artifacts_deduped": 1,
        "candidates_created": 0,
        "status": "received",
    }
    rows = env.APP_DB.connection.execute(
        "SELECT source_type, artifact_count, deduped_count FROM cf_memory_import_runs WHERE uid = ? AND run_id = ?",
        ("import-user", "client-run-1"),
    ).fetchall()
    assert [tuple(row) for row in rows] == [("pocket_notes", 2, 1)]
    artifacts = env.APP_DB.connection.execute(
        "SELECT source_type, redacted_body, metadata_json "
        "FROM cf_memory_import_artifacts WHERE uid = ? ORDER BY artifact_id",
        ("import-user",),
    ).fetchall()
    assert len(artifacts) == 2
    assert all(row[0] == "pocket_notes" for row in artifacts)
    assert all(row[1] is None for row in artifacts)

    repeated = asyncio.run(create_memory_import_batch(FakeRequest(env, signed_headers(secret), request)))
    assert repeated["artifacts_created"] == 0
    assert repeated["artifacts_deduped"] == 3


def test_memory_import_batch_rejects_unauthenticated_and_invalid_artifacts():
    secret = "memory-import-secret"
    env = make_env(secret)
    unauthenticated = asyncio.run(
        create_memory_import_batch(
            FakeRequest(env, {}, {"source_type": "notes", "items": []}),
        )
    )
    assert unauthenticated.status_code == 401

    invalid = asyncio.run(
        create_memory_import_batch(
            FakeRequest(
                env,
                signed_headers(secret),
                {"source_type": "notes", "items": [{"metadata": {"empty": True}}]},
            ),
        )
    )
    assert invalid.status_code == 400


def test_memory_import_batch_rejects_oversized_requests():
    secret = "memory-import-secret"
    env = make_env(secret)
    request = FakeRequest(env, signed_headers(secret), {"source_type": "notes", "items": []})
    request._body = b"{" + b"x" * 4_000_000 + b"}"

    response = asyncio.run(create_memory_import_batch(request))

    assert response.status_code == 413


def test_memory_import_batch_fails_closed_when_account_deletion_is_fenced():
    secret = "memory-import-secret"
    env = make_env(secret)
    env.APP_DB.connection.execute(
        "INSERT INTO cf_account_deletion_intents "
        "(uid, job_id, status, phase, next_attempt_at, created_at, updated_at) "
        "VALUES (?, ?, 'pending', 'quiescing', 0, 0, 0)",
        ("import-user", "delete-job"),
    )
    env.APP_DB.connection.commit()

    response = asyncio.run(
        create_memory_import_batch(
            FakeRequest(
                env,
                signed_headers(secret),
                {"source_type": "notes", "items": [{"external_id": "1"}]},
            ),
        )
    )
    assert response.status_code == 503
    count = env.APP_DB.connection.execute(
        "SELECT COUNT(*) FROM cf_memory_import_artifacts WHERE uid = ?",
        ("import-user",),
    ).fetchone()[0]
    assert count == 0
