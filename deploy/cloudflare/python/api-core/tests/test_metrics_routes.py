import asyncio
import json
import sqlite3
from types import SimpleNamespace

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from metrics_routes import get_metrics  # noqa: E402


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
        rows = self.connection.execute(self.sql, self.args).fetchall()
        return {"results": [dict(row) for row in rows]}


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for migration in sorted(migration_dir.glob("*.sql")):
            self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeRequest:
    def __init__(self, env, headers=None):
        self.scope = {"env": env}
        self.headers = headers or {}


def _body(response):
    return json.loads(response.body.decode("utf-8"))


def test_metrics_boundary_requires_the_operational_bearer_secret():
    env = SimpleNamespace(METRICS_SECRET="metrics-test-secret")

    missing = asyncio.run(get_metrics(FakeRequest(env)))
    assert missing.status_code == 401
    assert _body(missing) == {"detail": "Unauthorized"}

    wrong = asyncio.run(get_metrics(FakeRequest(env, {"authorization": "Bearer wrong"})))
    assert wrong.status_code == 401
    assert _body(wrong) == {"detail": "Unauthorized"}


def test_metrics_boundary_fails_closed_without_the_d1_authority():
    env = SimpleNamespace(METRICS_SECRET="metrics-test-secret")
    response = asyncio.run(get_metrics(FakeRequest(env, {"authorization": "Bearer metrics-test-secret"})))

    assert response.status_code == 503
    assert _body(response) == {"error": "metrics_unavailable"}
    assert response.headers["cache-control"] == "no-store"


def test_metrics_boundary_does_not_disclose_or_accept_when_secret_is_unconfigured():
    response = asyncio.run(
        get_metrics(FakeRequest(SimpleNamespace(), {"authorization": "Bearer caller-supplied"}))
    )

    assert response.status_code == 503
    assert _body(response) == {"error": "metrics_unavailable"}


def test_metrics_scrape_reports_live_d1_operational_state():
    db = FakeDb()
    now = 1_000_000
    db.connection.execute(
        "INSERT INTO cf_notification_outbox (notification_id, source_kind, source_id, uid, title, body, "
        "data_json, status, attempts, not_before, created_at, updated_at) "
        "VALUES ('n-1', 'integration', 's-1', 'user-1', 't', 'b', '{}', 'pending', 0, 0, 1, 1)"
    )
    db.connection.execute(
        "INSERT INTO cf_vector_projection_outbox (uid, source_kind, source_id, desired_version, operation, "
        "attempts, next_attempt_at, created_at, updated_at) "
        "VALUES ('user-1', 'memory', 'm-1', 1, 'delete', 0, 0, ?, ?)",
        (now - 120, now - 120),
    )
    db.connection.execute(
        "INSERT INTO cf_app_catalog (id, approved, status, disabled, data_json, updated_at, owner_uid) "
        "VALUES ('metrics-app', 1, 'approved', 0, '{}', 1, 'owner-1')"
    )
    db.connection.execute(
        "INSERT INTO cf_app_webhook_health (app_id, endpoint, first_failure_at, last_failure_at, "
        "failure_count, last_status, last_error, disabled, updated_at) "
        "VALUES ('metrics-app', 'integration', 1, 2, 5, 503, 'HTTP 503', 1, 2)"
    )
    db.connection.commit()

    env = SimpleNamespace(METRICS_SECRET="metrics-test-secret", APP_DB=db)
    response = asyncio.run(
        get_metrics(FakeRequest(env, {"authorization": "Bearer metrics-test-secret"}))
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.body.decode("utf-8")
    assert 'omi_notification_outbox_total{status="pending"} 1' in body
    assert "omi_vector_projection_outbox_depth 1" in body
    assert "omi_app_webhooks_disabled_total 1" in body
    assert "omi_account_deletion_intents_total 0" in body
    # The oldest-projection age is computed from real wall time, so only its
    # presence and non-negativity are asserted.
    age_line = next(
        line for line in body.splitlines() if line.startswith("omi_vector_projection_outbox_oldest_age_seconds ")
    )
    assert int(age_line.rsplit(" ", 1)[1]) >= 0
    assert "# HELP omi_metrics_scrape_timestamp_seconds" in body
