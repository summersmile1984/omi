import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from workstream_routes import (  # noqa: E402
    append_workstream_event,
    create_artifact_descriptor,
    get_workstream_detail,
    list_artifact_descriptors,
    list_continuation_checkpoints,
    list_workstream_events,
    resolve_work_intent,
    transition_artifact_status,
    update_workstream,
    upsert_continuation_checkpoint,
)
from goal_routes import create_goal


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migrations = [
            "0016_action_items.sql",
            "0018_goals.sql",
            "0023_goal_progress_history.sql",
            "0024_goal_mutations.sql",
            "0025_goal_progress_events.sql",
            "0026_workstreams.sql",
        ]
        for name in migrations:
            migration = Path(__file__).parents[3] / f"migrations/app/{name}"
            self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        self.connection.execute("BEGIN")
        try:
            for statement in statements:
                self.connection.execute(statement.sql, statement.args)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return []


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


def signed_headers(secret: str, uid: str = "workstream-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "workstream-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def mutation_headers(secret: str, key: str, generation: int = 0):
    headers = signed_headers(secret)
    headers.update({"idempotency-key": key, "x-account-generation": str(generation)})
    return headers


def test_goal_work_intent_workstream_events_artifacts_and_checkpoints_are_d1_backed():
    secret = "workstream-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    goal = asyncio.run(create_goal(FakeRequest(env, headers, {"title": "Finish the book"})))
    intent_body = {
        "origin": "goal",
        "goal_id": goal["id"],
        "title": "Write the book",
        "objective": "Turn the outline into a finished manuscript",
        "anchor_task_description": "Draft the opening chapter",
    }
    intent_headers = mutation_headers(secret, "intent-1")
    intent = asyncio.run(resolve_work_intent(FakeRequest(env, intent_headers, intent_body)))
    assert intent["receipt_id"].startswith("intent_")
    assert intent["workstream_id"].startswith("workstream_")
    assert intent["task_id"].startswith("task_")
    assert intent["newly_created"] is True
    assert asyncio.run(resolve_work_intent(FakeRequest(env, intent_headers, intent_body))) == intent
    conflict = asyncio.run(resolve_work_intent(FakeRequest(env, intent_headers, {**intent_body, "title": "Conflict"})))
    assert conflict.status_code == 409
    task_generation_conflict = asyncio.run(
        resolve_work_intent(
            FakeRequest(
                env,
                mutation_headers(secret, "task-intent-generation-conflict", generation=1),
                {"origin": "task", "task_id": intent["task_id"]},
            )
        )
    )
    assert task_generation_conflict.status_code == 409

    detail = asyncio.run(get_workstream_detail(FakeRequest(env, headers), intent["workstream_id"]))
    assert detail["workstream"]["status"] == "open"
    assert [event["sequence"] for event in detail["recent_events"]] == [1]
    assert detail["tasks"][0]["workstream_id"] == intent["workstream_id"]
    assert detail["artifacts"] == []
    assert detail["checkpoints"] == []

    event_body = {
        "kind": "decision",
        "summary": "Chose the opening chapter structure",
        "evidence_refs": [{"kind": "external", "id": "outline-1", "scope": "canonical"}],
        "sensitivity": "normal",
    }
    event_headers = mutation_headers(secret, "event-1")
    event = asyncio.run(append_workstream_event(FakeRequest(env, event_headers, event_body), intent["workstream_id"]))
    assert event["sequence"] == 2
    assert event["kind"] == "decision"
    assert (
        asyncio.run(append_workstream_event(FakeRequest(env, event_headers, event_body), intent["workstream_id"]))
        == event
    )
    assert [
        item["sequence"]
        for item in asyncio.run(
            list_workstream_events(FakeRequest(env, headers, query={"after_sequence": "1"}), intent["workstream_id"])
        )
    ] == [2]

    updated = asyncio.run(
        update_workstream(
            FakeRequest(env, mutation_headers(secret, "update-1"), {"title": "Write the full book"}),
            intent["workstream_id"],
        )
    )
    assert updated["title"] == "Write the full book"
    assert (
        asyncio.run(
            update_workstream(
                FakeRequest(env, mutation_headers(secret, "update-1"), {"title": "Write the full book"}),
                intent["workstream_id"],
            )
        )
        == updated
    )

    artifact_body = {
        "logical_key": "manuscript",
        "version": 1,
        "kind": "document",
        "uri": "r2://staging/manuscript-v1",
        "content_hash": "a" * 64,
        "evidence_refs": [{"kind": "external", "id": "outline-1", "scope": "canonical"}],
    }
    artifact = asyncio.run(
        create_artifact_descriptor(
            FakeRequest(env, mutation_headers(secret, "artifact-1"), artifact_body), intent["workstream_id"]
        )
    )
    assert artifact["version"] == 1
    assert artifact["status"] == "draft"
    assert asyncio.run(list_artifact_descriptors(FakeRequest(env, headers), intent["workstream_id"])) == [artifact]
    approved = asyncio.run(
        transition_artifact_status(
            FakeRequest(env, mutation_headers(secret, "artifact-status-1"), {"status": "awaiting_review"}),
            intent["workstream_id"],
            artifact["artifact_id"],
        )
    )
    assert approved["status"] == "awaiting_review"

    checkpoint_body = {
        "runtime_id": "agent-1",
        "last_event_sequence": 3,
        "context_summary": "Opening chapter structure selected",
        "evidence_refs": [],
    }
    checkpoint = asyncio.run(
        upsert_continuation_checkpoint(
            FakeRequest(env, mutation_headers(secret, "checkpoint-1"), checkpoint_body),
            intent["workstream_id"],
            "agent-1",
        )
    )
    assert checkpoint["last_event_sequence"] == 3
    assert asyncio.run(list_continuation_checkpoints(FakeRequest(env, headers), intent["workstream_id"])) == [
        checkpoint
    ]


def test_workstream_routes_fail_closed_on_missing_headers_and_invalid_boundaries():
    secret = "workstream-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    missing_headers = asyncio.run(
        append_workstream_event(
            FakeRequest(env, signed_headers(secret), {"kind": "system", "summary": "No receipt"}), "missing"
        )
    )
    assert missing_headers.status_code == 400
    invalid_event = asyncio.run(
        append_workstream_event(
            FakeRequest(env, mutation_headers(secret, "invalid"), {"kind": "invalid", "summary": "bad"}), "missing"
        )
    )
    assert invalid_event.status_code == 400
    invalid_limit = asyncio.run(
        list_workstream_events(FakeRequest(env, signed_headers(secret), query={"limit": "0"}), "missing")
    )
    assert invalid_limit.status_code == 400
