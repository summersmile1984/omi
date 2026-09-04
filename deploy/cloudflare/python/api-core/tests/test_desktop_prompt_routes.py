"""The real ASGI handler and SQLite migration, with the upstream response contract."""

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import entry  # noqa: E402
from internal_auth import create_request_context  # noqa: E402

PATH = "/v2/desktop/prompts"
SECRET = "desktop-prompt-test-secret"


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        migration = Path(__file__).parents[3] / "migrations/app/0153_desktop_prompts.sql"
        self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        connection = self.connection

        class Statement:
            async def all(self):
                return {"results": [dict(row) for row in connection.execute(sql).fetchall()]}

        return Statement()

    def seed(self, id, active=1, **document):
        self.connection.execute(
            "INSERT INTO cf_desktop_prompts (id, active, document_json) VALUES (?, ?, ?)",
            (id, active, json.dumps(document)),
        )
        self.connection.commit()


def client_for(db):
    env = SimpleNamespace(APP_DB=db, INTERNAL_ASSERTION_SECRET=SECRET)

    async def application(scope, receive, send):
        if scope["type"] == "http":
            scope["env"] = env
        await entry.app(scope, receive, send)

    return TestClient(application)


def headers(uid="prompt-user", path=PATH):
    encoded, signature = create_request_context(
        uid, SECRET, audience="api-core", method="GET", path=path, request_id="prompt-test", authority="better-auth"
    )
    return {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature}


def test_prompt_read_preserves_upstream_audience_defaults_sorting_and_projection():
    # Expectations come from backend/routers/desktop_prompts.py, including the
    # zero-build bypass and SHA256(prompt_id:uid) percentage assignment.
    db = Database()
    db.seed("z-banner", type="banner", question="Hello", audience={"min_build": 200})
    db.seed("a-choice", type="choice", question="Choose", options=list(range(9)), private_note="never expose")
    db.seed("inactive", active=0, type="stars", question="Not active")
    db.seed("beta", type="stars", question="Beta", audience={"channels": ["beta"]})
    db.seed("invalid", type="unknown", question="Invalid prompt")
    db.seed("rollout", type="nps", question="Rolled out", audience={"rollout_pct": 0})
    with client_for(db) as client:
        result = client.get(PATH, headers=headers()).json()
        assert [row["id"] for row in result["prompts"]] == ["a-choice", "z-banner"]
        assert result["prompts"][0] == {
            "id": "a-choice",
            "type": "choice",
            "question": "Choose",
            "options": ["0", "1", "2", "3", "4", "5"],
            "cta_label": None,
            "cta_url": None,
            "trigger_kind": "app_launch",
            "trigger_count": 0,
            "max_per_day": 1,
        }
        limited = client.get(PATH + "?channel=stable&build=100", headers=headers()).json()
        assert [row["id"] for row in limited["prompts"]] == ["a-choice"]
        assert client.get(PATH + "?build=invalid", headers=headers()).status_code == 422
    db.connection.close()


def test_prompt_rollout_is_stable_per_user_and_empty_legacy_state_is_valid():
    db = Database()
    with client_for(db) as client:
        empty = client.get(PATH, headers=headers())
        assert empty.status_code == 200
        assert empty.json() == {"prompts": []}
        db.seed("rollout", type="nps", question="How likely?", audience={"rollout_pct": 50})
        for uid in ("prompt-user", "other-user"):
            expected = int(hashlib.sha256(f"rollout:{uid}".encode()).hexdigest()[:8], 16) % 100 < 50
            for _ in range(2):
                result = client.get(PATH, headers=headers(uid)).json()
                assert bool(result["prompts"]) is expected
    db.connection.close()


def test_authentication_and_storage_failure_cannot_look_like_empty_success():
    with client_for(object()) as client:
        assert client.get(PATH).status_code == 401
        assert client.get(PATH, headers=headers(path="/wrong-route")).status_code == 401
        failed = client.get(PATH, headers=headers())
        assert failed.status_code == 503
        assert failed.json() == {"detail": "Desktop prompts unavailable"}

    db = Database()
    db.seed("broken", type="banner", question="Bad config", audience="invalid")
    with client_for(db) as client:
        assert client.get(PATH, headers=headers()).status_code == 503
    db.connection.close()
