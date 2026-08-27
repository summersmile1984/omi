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
    append_goal_progress_event,
    create_canonical_goal,
    create_goal,
    delete_goal,
    focus_goal,
    get_current_goal,
    get_goal,
    get_goal_detail,
    get_goal_history,
    list_goals,
    list_goal_progress_events,
    list_canonical_goals,
    transition_goal_lifecycle,
    unfocus_goal,
    update_goal,
    update_goal_progress,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        action_items_migration = Path(__file__).parents[3] / "migrations/app/0016_action_items.sql"
        self.connection.executescript(action_items_migration.read_text())
        migration = Path(__file__).parents[3] / "migrations/app/0018_goals.sql"
        self.connection.executescript(migration.read_text())
        history_migration = Path(__file__).parents[3] / "migrations/app/0023_goal_progress_history.sql"
        self.connection.executescript(history_migration.read_text())
        mutation_migration = Path(__file__).parents[3] / "migrations/app/0024_goal_mutations.sql"
        self.connection.executescript(mutation_migration.read_text())
        events_migration = Path(__file__).parents[3] / "migrations/app/0025_goal_progress_events.sql"
        self.connection.executescript(events_migration.read_text())
        workstream_migration = Path(__file__).parents[3] / "migrations/app/0026_workstreams.sql"
        self.connection.executescript(workstream_migration.read_text())

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


def mutation_headers(secret: str, key: str, generation: int = 0, uid: str = "goal-user"):
    headers = signed_headers(secret, uid)
    headers.update({"idempotency-key": key, "x-account-generation": str(generation)})
    return headers


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

    history = asyncio.run(get_goal_history(FakeRequest(env, headers, query={"days": "30"}), created["id"]))
    assert len(history) == 1
    assert history[0]["value"] == 25
    events = asyncio.run(list_goal_progress_events(FakeRequest(env, headers), created["id"]))
    assert len(events) == 1
    assert events[0]["sequence"] == 1
    assert events[0]["kind"] == "metric_update"
    assert events[0]["metric"]["current"] == 25
    asyncio.run(update_goal_progress(FakeRequest(env, headers, query={"current_value": "30"}), created["id"]))
    history = asyncio.run(get_goal_history(FakeRequest(env, headers), created["id"]))
    assert len(history) == 1
    assert history[0]["value"] == 30
    events = asyncio.run(list_goal_progress_events(FakeRequest(env, headers), created["id"]))
    assert [event["sequence"] for event in events] == [2, 1]

    other = asyncio.run(get_goal(FakeRequest(env, signed_headers(secret, "other-user")), created["id"]))
    assert other.status_code == 404

    deleted = asyncio.run(delete_goal(FakeRequest(env, headers), created["id"]))
    assert deleted == {"success": True, "deleted_id": created["id"]}
    assert asyncio.run(get_current_goal(FakeRequest(env, headers))) is None
    ended = asyncio.run(list_goals(FakeRequest(env, headers, query={"include_ended": "true"})))
    assert ended[0]["status"] == "abandoned"
    assert ended[0]["is_active"] is False


def test_canonical_goal_create_and_list_use_generation_scoped_receipt():
    secret = "goal-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    body = {
        "title": "Canonical goal",
        "desired_outcome": "A generation-safe goal",
        "metric": {"type": "numeric", "current": 0, "target": 5, "unit": "steps"},
    }
    headers = mutation_headers(secret, "canonical-create", generation=3)
    created = asyncio.run(create_canonical_goal(FakeRequest(env, headers, body)))
    assert created["id"].startswith("goal_")
    assert created["metric"]["target"] == 5
    replay = asyncio.run(create_canonical_goal(FakeRequest(env, headers, body)))
    assert replay == created
    conflict = asyncio.run(create_canonical_goal(FakeRequest(env, headers, {**body, "title": "Conflict"})))
    assert conflict.status_code == 409
    listed = asyncio.run(list_canonical_goals(FakeRequest(env, signed_headers(secret))))
    assert [goal["id"] for goal in listed] == [created["id"]]
    missing_headers = asyncio.run(create_canonical_goal(FakeRequest(env, signed_headers(secret), body)))
    assert missing_headers.status_code == 400


def test_goal_detail_composes_uid_scoped_d1_projections():
    secret = "goal-detail-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    goal = asyncio.run(create_goal(FakeRequest(env, headers, {"title": "Ship the integration"})))
    uid = "goal-user"
    now = 1_700_000_000
    env.APP_DB.connection.executemany(
        "INSERT INTO cf_workstreams "
        "(uid, id, goal_id, title, objective, status, current_state_summary, latest_event_sequence, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (uid, "ws-open", goal["id"], "Open thread", "Finish the integration", "open", "In progress", 2, now, now + 2),
            (uid, "ws-archived", goal["id"], "Old thread", "No longer active", "archived", "", 1, now, now + 1),
            ("other-user", "ws-foreign", goal["id"], "Foreign thread", "Do not leak", "open", "", 1, now, now + 3),
        ],
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_action_items "
        "(uid, id, description, status, completed, goal_id, workstream_id, owner, source, created_at, updated_at) "
        "VALUES (?, ?, ?, 'active', 0, ?, ?, 'user', 'manual', ?, ?)",
        (uid, "task-open", "Verify the Worker path", goal["id"], "ws-open", now, now),
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_action_items "
        "(uid, id, description, status, completed, goal_id, deleted, owner, source, created_at, updated_at) "
        "VALUES (?, ?, ?, 'cancelled', 0, ?, 1, 'user', 'manual', ?, ?)",
        (uid, "task-deleted", "Hidden task", goal["id"], now, now),
    )
    env.APP_DB.connection.commit()
    event = asyncio.run(
        append_goal_progress_event(
            FakeRequest(
                env,
                mutation_headers(secret, "detail-event"),
                {"kind": "evidence", "summary": "Worker route verified", "evidence_refs": []},
            ),
            goal["id"],
        )
    )

    detail = asyncio.run(get_goal_detail(FakeRequest(env, headers), goal["id"]))
    assert detail["goal"]["id"] == goal["id"]
    assert [thread["workstream_id"] for thread in detail["active_threads"]] == ["ws-open"]
    assert [task["id"] for task in detail["tasks"]] == ["task-open"]
    assert detail["progress_events"][0]["event_id"] == event["event_id"]
    assert asyncio.run(get_goal_detail(FakeRequest(env, signed_headers(secret, "other-user")), goal["id"])).status_code == 404


def test_goal_routes_reject_invalid_progress_and_empty_update():
    secret = "goal-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    created = asyncio.run(create_goal(FakeRequest(env, headers, {"title": "Read more"})))

    invalid_progress = asyncio.run(update_goal_progress(FakeRequest(env, headers), created["id"]))
    assert invalid_progress.status_code == 400
    invalid_update = asyncio.run(update_goal(FakeRequest(env, headers, {}), created["id"]))
    assert invalid_update.status_code == 400
    invalid_days = asyncio.run(get_goal_history(FakeRequest(env, headers, query={"days": "0"}), created["id"]))
    assert invalid_days.status_code == 400


def test_goal_progress_event_append_list_and_receipt_idempotency():
    secret = "goal-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)
    created = asyncio.run(create_goal(FakeRequest(env, headers, {"title": "Ship the chapter"})))
    event_body = {
        "kind": "milestone",
        "summary": "Finished the first chapter",
        "evidence_refs": [{"kind": "external", "id": "note-1", "scope": "canonical"}],
        "metric": {"type": "numeric", "current": 1, "target": 10, "unit": "chapters"},
    }
    event_headers = mutation_headers(secret, "event-1")
    appended = asyncio.run(append_goal_progress_event(FakeRequest(env, event_headers, event_body), created["id"]))
    assert appended["event_id"].startswith("gpe_")
    assert appended["sequence"] == 1
    assert appended["kind"] == "milestone"
    assert appended["evidence_refs"][0]["scope"] == "canonical"
    assert appended["metric"]["current"] == 1

    replay = asyncio.run(append_goal_progress_event(FakeRequest(env, event_headers, event_body), created["id"]))
    assert replay == appended
    conflict = asyncio.run(
        append_goal_progress_event(
            FakeRequest(env, event_headers, {**event_body, "summary": "different"}),
            created["id"],
        )
    )
    assert conflict.status_code == 409

    listed = asyncio.run(list_goal_progress_events(FakeRequest(env, headers, query={"limit": "1"}), created["id"]))
    assert listed == [appended]
    assert asyncio.run(get_goal(FakeRequest(env, headers), created["id"]))["metric"]["current"] == 1

    missing_headers = asyncio.run(append_goal_progress_event(FakeRequest(env, headers, event_body), created["id"]))
    assert missing_headers.status_code == 400
    invalid_scope = asyncio.run(
        append_goal_progress_event(
            FakeRequest(
                env,
                mutation_headers(secret, "event-invalid-scope"),
                {
                    "kind": "evidence",
                    "summary": "Local evidence",
                    "evidence_refs": [{"kind": "screen", "id": "screen-1", "scope": "device_local"}],
                },
            ),
            created["id"],
        )
    )
    assert invalid_scope.status_code == 400
    assert (
        asyncio.run(
            list_goal_progress_events(FakeRequest(env, headers, query={"limit": "0"}), created["id"])
        ).status_code
        == 400
    )


def test_goal_focus_cap_lifecycle_and_idempotency_are_d1_scoped():
    secret = "goal-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    goals = [create_goal(FakeRequest(env, signed_headers(secret), {"title": f"Goal {index}"})) for index in range(6)]
    created = [asyncio.run(goal) if asyncio.iscoroutine(goal) else goal for goal in goals]

    focused = []
    for index, goal in enumerate(created[:5]):
        response = asyncio.run(
            focus_goal(FakeRequest(env, mutation_headers(secret, f"focus-{index}"), {"focus_rank": None}), goal["id"])
        )
        assert response["status"] == "focused"
        assert response["focus_rank"] == index
        focused.append(response)

    full = asyncio.run(focus_goal(FakeRequest(env, mutation_headers(secret, "focus-six"), {}), created[5]["id"]))
    assert full.status_code == 409
    replacement = asyncio.run(
        focus_goal(
            FakeRequest(
                env,
                mutation_headers(secret, "focus-six-replace"),
                {"replacement_goal_id": created[0]["id"]},
            ),
            created[5]["id"],
        )
    )
    assert replacement["status"] == "focused"
    assert replacement["focus_rank"] == 0
    assert (
        asyncio.run(
            get_goal(
                FakeRequest(
                    env,
                    signed_headers(secret),
                ),
                created[0]["id"],
            )
        )["status"]
        == "background"
    )

    retry = asyncio.run(
        focus_goal(
            FakeRequest(env, mutation_headers(secret, "focus-six-replace"), {"replacement_goal_id": created[0]["id"]}),
            created[5]["id"],
        )
    )
    assert retry == replacement
    conflict = asyncio.run(
        focus_goal(FakeRequest(env, mutation_headers(secret, "focus-six-replace"), {"focus_rank": 1}), created[5]["id"])
    )
    assert conflict.status_code == 409

    unfocused = asyncio.run(unfocus_goal(FakeRequest(env, mutation_headers(secret, "unfocus-six")), created[5]["id"]))
    assert unfocused["status"] == "background"
    paused = asyncio.run(
        transition_goal_lifecycle(
            FakeRequest(
                env,
                mutation_headers(secret, "pause-six"),
                {"status": "paused", "relationship_disposition": "retain"},
            ),
            created[5]["id"],
        )
    )
    assert paused["status"] == "paused"
    ended = asyncio.run(
        transition_goal_lifecycle(
            FakeRequest(
                env,
                mutation_headers(secret, "end-six"),
                {"status": "achieved", "relationship_disposition": "retain"},
            ),
            created[5]["id"],
        )
    )
    assert ended["status"] == "achieved"
    assert ended["is_active"] is False
    detached = asyncio.run(
        transition_goal_lifecycle(
            FakeRequest(
                env,
                mutation_headers(secret, "detach-six"),
                {"status": "abandoned", "relationship_disposition": "detach"},
            ),
            created[5]["id"],
        )
    )
    assert detached.status_code == 409
    missing_headers = asyncio.run(focus_goal(FakeRequest(env, signed_headers(secret), {}), created[1]["id"]))
    assert missing_headers.status_code == 400
