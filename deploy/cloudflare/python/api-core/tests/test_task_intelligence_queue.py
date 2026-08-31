import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from task_intelligence_routes import evaluate_what_matters_now  # noqa: E402


SECRET = "task-intelligence-queue-secret"
UID = "task-queue-user"


class FakeStatement:
    def __init__(self, connection, sql, args=()):
        self.connection = connection
        self.sql = sql
        self.args = args

    def bind(self, *args):
        return FakeStatement(self.connection, self.sql, args)

    async def first(self):
        row = self.connection.execute(self.sql, self.args).fetchone()
        return dict(row) if row is not None else None

    async def all(self):
        return {"results": [dict(row) for row in self.connection.execute(self.sql, self.args).fetchall()]}

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for migration in sorted(migration_dir.glob("*.sql")):
            self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            results = []
            for statement in statements:
                cursor = self.connection.execute(statement.sql, statement.args)
                results.append({"meta": {"changes": cursor.rowcount}})
            self.connection.commit()
            return results
        except Exception:
            self.connection.rollback()
            raise


class FakeQueue:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


class FailingAi:
    async def run(self, _model, _input):
        raise RuntimeError("provider unavailable")


class FakeRequest:
    def __init__(self, env, *, headers, body=None):
        self.scope = {"env": env}
        self.headers = headers
        self.query_params = {}
        self._body = json.dumps(body or {}).encode()

    async def body(self):
        return self._body


def auth_headers(*, authority="better-auth"):
    encoded = base64.urlsafe_b64encode(
        json.dumps({"uid": UID, "authority": authority, "requestId": "task-queue-test"}, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
        "x-account-generation": "1",
    }


def environment():
    database = FakeDb()
    queue = FakeQueue()
    env = SimpleNamespace(APP_DB=database, JOBS=queue, AI=FailingAi(), INTERNAL_ASSERTION_SECRET=SECRET)
    database.connection.execute(
        "INSERT INTO cf_account_cutover "
        "(uid, state, checkpoint_phase, destination_backend_bound, account_generation, updated_at) "
        "VALUES (?, 'new', 'completed', 1, 1, 1)",
        (UID,),
    )
    database.connection.execute(
        "INSERT INTO cf_task_candidates "
        "(uid, candidate_id, account_generation, status, description, evidence_refs_json, "
        "request_fingerprint, created_at, updated_at) VALUES (?, 'candidate-1', 1, 'pending', 'Queue retry', '[]', 'candidate-fp', 1, 1)",
        (UID,),
    )
    database.connection.commit()
    return env, queue


def test_provider_failure_releases_lease_to_queue_with_bounded_retry():
    env, queue = environment()
    response = asyncio.run(
        evaluate_what_matters_now(
            FakeRequest(env, headers=auth_headers()),
        )
    )

    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["status"] == "queued"
    assert payload["retryable"] is True
    assert payload["queue_enqueued"] is True
    assert len(queue.messages) == 1
    assert queue.messages[0]["kind"] == "task_intelligence_evaluate"
    job = env.APP_DB.connection.execute(
        "SELECT status, attempts, lease_token, next_attempt_at, input_json "
        "FROM cf_task_intelligence_jobs"
    ).fetchone()
    assert job["status"] == "queued"
    assert job["attempts"] == 1
    assert job["lease_token"] is None
    assert job["next_attempt_at"] > 1
    assert json.loads(job["input_json"])["source_rows"][0]["row"]["candidate_id"] == "candidate-1"
