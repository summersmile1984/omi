import sqlite3
from pathlib import Path

import pytest


MIGRATION = Path(__file__).parents[3] / "migrations" / "app" / "0109_task_intelligence.sql"
TASK_TABLES = (
    "cf_task_candidates",
    "cf_task_interventions",
    "cf_task_feedback",
    "cf_task_outcomes",
    "cf_task_context_snapshots",
    "cf_task_open_loop_snapshots",
    "cf_task_intelligence_jobs",
    "cf_task_llm_receipts",
    "cf_task_evaluations",
)


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    # The full migration chain creates these tables before 0109.  Keeping the
    # fixture small makes this test exercise only the task migration itself.
    connection.executescript(
        """
        CREATE TABLE cf_account_deletion_intents (
          uid TEXT PRIMARY KEY,
          job_id TEXT,
          status TEXT,
          phase TEXT,
          next_attempt_at INTEGER
        );
        CREATE TABLE cf_account_deletion_tombstones (
          uid TEXT PRIMARY KEY,
          completed_at INTEGER,
          expires_at INTEGER
        );
        """
    )
    connection.executescript(MIGRATION.read_text())
    return connection


def _insert_row(connection: sqlite3.Connection, table: str, uid: str, suffix: str) -> None:
    now = 1_700_000_000
    fingerprint = f"fingerprint-{suffix}"
    if table == "cf_task_candidates":
        connection.execute(
            "INSERT INTO cf_task_candidates "
            "(uid, candidate_id, account_generation, status, description, evidence_refs_json, "
            "request_fingerprint, created_at, updated_at) VALUES (?, ?, 1, 'pending', ?, '[]', ?, ?, ?)",
            (uid, f"candidate-{suffix}", "Candidate", fingerprint, now, now),
        )
    elif table == "cf_task_interventions":
        connection.execute(
            "INSERT INTO cf_task_interventions "
            "(uid, intervention_id, account_generation, attribution_chain_id, request_fingerprint, payload_json, created_at) "
            "VALUES (?, ?, 1, ?, ?, '{}', ?)",
            (uid, f"intervention-{suffix}", f"chain-{suffix}", fingerprint, now),
        )
    elif table == "cf_task_feedback":
        connection.execute(
            "INSERT INTO cf_task_feedback "
            "(uid, feedback_id, account_generation, request_fingerprint, payload_json, created_at) "
            "VALUES (?, ?, 1, ?, '{}', ?)",
            (uid, f"feedback-{suffix}", fingerprint, now),
        )
    elif table == "cf_task_outcomes":
        connection.execute(
            "INSERT INTO cf_task_outcomes "
            "(uid, outcome_id, account_generation, attribution_chain_id, request_fingerprint, payload_json, occurred_at) "
            "VALUES (?, ?, 1, ?, ?, '{}', ?)",
            (uid, f"outcome-{suffix}", f"chain-{suffix}", fingerprint, now),
        )
    elif table in {"cf_task_context_snapshots", "cf_task_open_loop_snapshots"}:
        connection.execute(
            f"INSERT INTO {table} "
            "(uid, device_id, account_generation, snapshot_id, request_fingerprint, payload_json, generated_at, expires_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?, '{}', ?, ?, ?)",
            (uid, f"device-{suffix}", f"snapshot-{suffix}", fingerprint, now, now + 60, now),
        )
    elif table == "cf_task_intelligence_jobs":
        connection.execute(
            "INSERT INTO cf_task_intelligence_jobs "
            "(uid, job_id, account_generation, device_id, request_fingerprint, status, next_attempt_at, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?, 'queued', ?, ?, ?)",
            (uid, f"job-{suffix}", f"device-{suffix}", fingerprint, now, now, now),
        )
    elif table == "cf_task_llm_receipts":
        connection.execute(
            "INSERT INTO cf_task_llm_receipts "
            "(uid, receipt_id, job_id, evaluation_id, account_generation, provider, model_version, "
            "request_fingerprint, response_fingerprint, status, created_at) "
            "VALUES (?, ?, ?, ?, 1, 'test', 'test-v1', ?, ?, 'completed', ?)",
            (uid, f"receipt-{suffix}", f"job-{suffix}", f"evaluation-{suffix}", fingerprint, f"response-{suffix}", now),
        )
    elif table == "cf_task_evaluations":
        connection.execute(
            "INSERT INTO cf_task_evaluations "
            "(uid, evaluation_id, job_id, account_generation, device_id, request_fingerprint, projection_json, generated_at, expires_at) "
            "VALUES (?, ?, ?, 1, ?, ?, '{}', ?, ?)",
            (uid, f"evaluation-{suffix}", f"job-{suffix}", f"device-{suffix}", fingerprint, now, now + 60),
        )
    else:  # pragma: no cover - TASK_TABLES is intentionally exhaustive.
        raise AssertionError(table)


def test_task_tables_have_insert_update_deletion_fences_and_delete_cleanup():
    connection = _database()

    trigger_names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'adf_%_task_%'"
        )
    }
    for table in TASK_TABLES:
        stem = table.removeprefix("cf_")
        assert f"adf_i_{stem}" in trigger_names
        assert f"adf_u_{stem}" in trigger_names
        _insert_row(connection, table, "race-user", f"initial-{stem}")

    connection.execute(
        "INSERT INTO cf_account_deletion_intents(uid, job_id, status, phase, next_attempt_at) "
        "VALUES ('race-user', 'delete-race', 'running', 'purging', 1)"
    )
    for table in TASK_TABLES:
        with pytest.raises(sqlite3.IntegrityError, match="account deletion fence"):
            _insert_row(connection, table, "race-user", f"late-insert-{table}")
        with pytest.raises(sqlite3.IntegrityError, match="account deletion fence"):
            connection.execute(f"UPDATE {table} SET uid = uid WHERE uid = 'race-user'")

    # The deletion owner remains allowed to purge every fenced table.
    for table in TASK_TABLES:
        connection.execute(f"DELETE FROM {table} WHERE uid = 'race-user'")
        assert connection.execute(f"SELECT COUNT(*) FROM {table} WHERE uid = 'race-user'").fetchone()[0] == 0

    for table in TASK_TABLES:
        _insert_row(connection, table, "tombstone-user", f"before-tombstone-{table}")
    connection.execute(
        "INSERT INTO cf_account_deletion_tombstones(uid, completed_at, expires_at) "
        "VALUES ('tombstone-user', 1, 2)"
    )
    for table in TASK_TABLES:
        with pytest.raises(sqlite3.IntegrityError, match="account deletion fence"):
            _insert_row(connection, table, "tombstone-user", f"late-tombstone-{table}")

