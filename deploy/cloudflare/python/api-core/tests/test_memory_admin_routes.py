import asyncio
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from memory_admin_routes import get_non_active_route_report  # noqa: E402


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


class FakeRequest:
    def __init__(self, env, headers, query=None):
        self.scope = {"env": env}
        self.headers = headers
        self.query_params = query or {}


def make_env(admin_key="admin-secret"):
    return type("Env", (), {"APP_DB": FakeDb(), "ADMIN_KEY": admin_key})()


def seed_cutover(env, uid="report-user", generation=1):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_account_cutover "
        "(uid, schema_version, state, account_generation, ui_generation, api_generation, stranded_new_data, "
        "offline_queue_instruction, checkpoint_phase, destination_backend_bound, updated_at) "
        "VALUES (?, 1, 'new', ?, ?, ?, 0, 'none', 'completed', 1, 1)",
        (uid, generation, generation, generation),
    )
    env.APP_DB.connection.commit()


def seed_row(
    env,
    *,
    uid="report-user",
    outcome_id,
    route="review",
    source_ids=None,
    run_id="run-1",
    generation=1,
    default_visible=0,
    audit_metadata=None,
):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_memory_non_active_routes "
        "(uid, outcome_id, route, idempotency_key, source_ids_json, reason, run_id, patch_id, "
        "audit_metadata_json, created_at, default_long_term_visible, payload_fingerprint, account_generation) "
        "VALUES (?, ?, ?, ?, ?, 'accounted', ?, NULL, ?, 100, ?, ?, ?)",
        (
            uid,
            outcome_id,
            route,
            f"key-{outcome_id}",
            json.dumps(source_ids or [f"source-{outcome_id}"]),
            run_id,
            json.dumps(audit_metadata or {}),
            default_visible,
            "a" * 64,
            generation,
        ),
    )
    env.APP_DB.connection.commit()


def test_non_active_report_reads_only_completed_d1_projection_for_requested_uid():
    env = make_env()
    seed_cutover(env)
    seed_row(env, outcome_id="outcome-1", source_ids=["source-1"], audit_metadata={"preserved": True})
    seed_row(env, uid="other-user", outcome_id="foreign")

    response = asyncio.run(
        get_non_active_route_report(
            FakeRequest(
                env,
                {"secret-key": "admin-secret"},
                {"run_id": "run-1", "expected_source_ids": "source-1"},
            ),
            "report-user",
        )
    )
    assert response["uid"] == "report-user"
    assert response["status"] == "green"
    assert response["total_accounted_outcomes"] == 1
    assert response["counts_by_route"] == {
        "review": 1,
        "archive": 0,
        "context_only": 0,
        "reject": 0,
        "hidden": 0,
        "skip": 0,
    }
    assert response["evidence"][0]["source_ids"] == ["source-1"]


def test_non_active_report_preserves_audit_red_flags_and_query_scope():
    env = make_env()
    seed_cutover(env)
    seed_row(env, outcome_id="one", source_ids=["source-1"], default_visible=1)
    seed_row(env, outcome_id="two", route="reject", source_ids=["source-1"])
    seed_row(env, outcome_id="other-run", run_id="other-run", source_ids=["source-2"])

    response = asyncio.run(
        get_non_active_route_report(
            FakeRequest(env, {"secret-key": "admin-secret"}, {"run_id": "run-1"}),
            "report-user",
        )
    )
    assert response["status"] == "red"
    assert response["total_accounted_outcomes"] == 2
    assert any("default Long-term visible" in reason for reason in response["red_reasons"])
    assert any("duplicate terminal outcomes" in reason for reason in response["red_reasons"])


def test_non_active_report_fails_closed_for_admin_key_cutover_and_deletion_fence():
    env = make_env()
    unauthorized = asyncio.run(get_non_active_route_report(FakeRequest(env, {}), "report-user"))
    assert unauthorized.status_code == 403

    missing_cutover = asyncio.run(
        get_non_active_route_report(FakeRequest(env, {"secret-key": "admin-secret"}), "report-user")
    )
    assert missing_cutover.status_code == 503

    seed_cutover(env)
    env.APP_DB.connection.execute(
        "INSERT INTO cf_account_deletion_intents "
        "(uid, job_id, status, phase, next_attempt_at, created_at, updated_at) "
        "VALUES ('report-user', 'delete-1', 'pending', 'quiescing', 1, 1, 1)"
    )
    env.APP_DB.connection.commit()
    deleting = asyncio.run(get_non_active_route_report(FakeRequest(env, {"secret-key": "admin-secret"}), "report-user"))
    assert deleting.status_code == 409
