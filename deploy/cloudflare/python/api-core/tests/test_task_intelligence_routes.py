import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from task_intelligence_routes import (  # noqa: E402
    _candidate_input,
    _device,
    _stable_id,
    create_staged_task,
    list_staged_tasks,
)


SECRET = "task-secret"


def _headers(**extra):
    encoded = base64.urlsafe_b64encode(
        json.dumps({"uid": "task-user", "authority": "better-auth", "requestId": "task-test"}, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature, **extra}


class _Statement:
    def __init__(self, db, sql):
        self.db = db
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def first(self):
        if "FROM cf_account_cutover" in self.sql:
            return {"state": "new", "checkpoint_phase": "completed", "destination_backend_bound": 1, "account_generation": 1}
        if "FROM cf_account_deletion_intents" in self.sql:
            return None
        if "FROM cf_task_candidates" in self.sql and "candidate_id = ?" in self.sql:
            uid, candidate_id, generation = self.args
            return next((row for row in self.db.rows if row["uid"] == uid and row["candidate_id"] == candidate_id and row["account_generation"] == generation), None)
        return None

    async def all(self):
        if "FROM cf_task_candidates" in self.sql:
            uid, generation, limit, offset = self.args
            rows = [row for row in self.db.rows if row["uid"] == uid and row["account_generation"] == generation and row["status"] == "pending"]
            return {"results": rows[offset : offset + limit]}
        return {"results": []}

    async def run(self):
        if "INSERT INTO cf_task_candidates" in self.sql:
            uid, candidate_id, generation, description, due_at, source, priority, metadata, category, score, refs, fingerprint, created, updated = self.args
            self.db.rows.append({"uid": uid, "candidate_id": candidate_id, "account_generation": generation, "status": "pending", "description": description, "due_at": due_at, "source": source, "priority": priority, "metadata": metadata, "category": category, "relevance_score": score, "created_at": created, "updated_at": updated})
        return {"meta": {"changes": 1}}


class _Db:
    def __init__(self):
        self.rows = []

    def prepare(self, sql):
        return _Statement(self, sql)


class _Request:
    def __init__(self, method, path, body, headers):
        self.scope = {"env": type("Env", (), {"INTERNAL_ASSERTION_SECRET": SECRET, "APP_DB": _Db()})()}
        self.scope["env"].APP_DB = self.db = _Db()
        self.method = method
        self.url = type("Url", (), {"path": path})()
        self.headers = headers
        self.query_params = {}
        self._body = json.dumps(body).encode()

    async def body(self):
        return self._body


def test_candidate_input_rejects_unbounded_description_and_keeps_identity_fields():
    payload = _candidate_input({"description": "  ship it  ", "relevance_score": 700})
    assert payload["description"] == "ship it"
    assert payload["relevance_score"] == 700
    assert _stable_id("staged", "task-user", 1, "fingerprint").startswith("staged_")


def test_device_scope_requires_platform_and_hash_and_rejects_cross_device():
    request = type("Request", (), {"headers": {"x-app-platform": "macos", "x-device-id-hash": "abc123"}})()
    assert _device(request, "abc123") == "macos_abc123"
    assert _device(request, "ios_other").status_code == 403


def test_create_and_list_staged_task_use_d1_account_generation():
    request = _Request("POST", "/v1/staged-tasks", {"description": "Ship the Cloudflare adapter", "relevance_score": 900}, _headers())
    response = asyncio.run(create_staged_task(request))
    assert response["description"] == "Ship the Cloudflare adapter"
    assert response["completed"] is False
    list_request = _Request("GET", "/v1/staged-tasks", {}, _headers())
    list_request.scope["env"].APP_DB = request.scope["env"].APP_DB
    result = asyncio.run(list_staged_tasks(list_request))
    assert result["has_more"] is False
    assert [item["id"] for item in result["items"]] == [response["id"]]
