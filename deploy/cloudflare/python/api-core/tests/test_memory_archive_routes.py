import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from memory_routes import search_archive_memory  # noqa: E402


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


def signed_headers(secret: str, uid: str = "archive-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "archive-test"},
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


def seed_authority(env, *, uid: str = "archive-user", archive_capability: int = 1, generation: int = 1):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_memory_global_read_gate "
        "(id, schema_version, source, memory_reads_enabled, kill_switch_active, updated_at) "
        "VALUES (1, 1, 'cloudflare_operator', 1, 0, 1)"
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_memory_control "
        "(uid, schema_version, source, memory_reads_enabled, default_memory_grant, archive_capability, "
        "account_generation, source_revision, updated_at) VALUES (?, 1, 'cloudflare_cutover_projection', 1, 1, ?, ?, 'r1', 1)",
        (uid, archive_capability, generation),
    )
    env.APP_DB.connection.commit()


def archive_row(
    env,
    *,
    uid: str = "archive-user",
    memory_id: str,
    content: str = "coffee archive record",
    status: str = "active",
    processing_state: str = "processed",
    source_state: str = "active",
    sensitivity_labels_json: str = "[]",
    is_locked: int = 0,
    account_generation: int = 1,
    memory_tier: str = "archive",
):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_memory_archive_items "
        "(uid, memory_id, memory_tier, content, version, status, processing_state, source_state, "
        "sensitivity_labels_json, visibility, user_asserted, captured_at, updated_at, item_revision, "
        "source_id, evidence_json, confidence, is_locked, account_generation, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, 'private', 0, 1, 2, 1, 'conversation-1', ?, 0.8, ?, ?, 1)",
        (
            uid,
            memory_id,
            memory_tier,
            content,
            status,
            processing_state,
            source_state,
            sensitivity_labels_json,
            '[{"source_id":"conversation-1"}]',
            is_locked,
            account_generation,
        ),
    )
    env.APP_DB.connection.commit()


def response_json(response):
    return json.loads(response.body.decode())


def test_archive_search_requires_explicit_intent_and_server_projection():
    secret = "archive-secret"
    env = make_env(secret)

    missing = asyncio.run(search_archive_memory(FakeRequest(env, signed_headers(secret), {"include_archive": "true"})))
    assert missing.status_code == 403
    assert response_json(missing)["reason"] == "missing_global_read_gate"

    seed_authority(env, archive_capability=0)
    denied = asyncio.run(search_archive_memory(FakeRequest(env, signed_headers(secret), {"include_archive": "true"})))
    assert denied.status_code == 403
    assert response_json(denied)["reason"] == "missing_archive_capability"

    no_intent = asyncio.run(search_archive_memory(FakeRequest(env, signed_headers(secret))))
    assert no_intent.status_code == 403
    assert response_json(no_intent)["reason"] == "missing_explicit_archive_request"

    malformed_intent = asyncio.run(
        search_archive_memory(FakeRequest(env, signed_headers(secret), {"include_archive": "maybe"}))
    )
    assert malformed_intent.status_code == 400


def test_archive_search_is_uid_generation_and_lifecycle_scoped():
    secret = "archive-secret"
    env = make_env(secret)
    seed_authority(env)
    archive_row(env, memory_id="visible", content="Coffee in the archive")
    archive_row(env, uid="other-user", memory_id="foreign", content="Coffee foreign archive")
    archive_row(env, memory_id="locked", is_locked=1)
    archive_row(env, memory_id="tombstoned", source_state="tombstoned")
    archive_row(env, memory_id="hidden", status="hidden")
    archive_row(env, memory_id="pending", processing_state="pending")
    archive_row(env, memory_id="blocked", processing_state="blocked")
    archive_row(env, memory_id="restricted", sensitivity_labels_json='["health"]')
    archive_row(env, memory_id="wrong-generation", account_generation=2)

    # A malformed legacy-shaped row must still be filtered by the archive-tier
    # predicate if it ever reaches the projection table.
    env.APP_DB.connection.execute("PRAGMA ignore_check_constraints = ON")
    archive_row(env, memory_id="not-archive", memory_tier="long_term")
    env.APP_DB.connection.execute("PRAGMA ignore_check_constraints = OFF")

    response = asyncio.run(
        search_archive_memory(
            FakeRequest(
                env,
                signed_headers(secret),
                {"include_archive": "true", "query": "coffee", "limit": "10", "offset": "0"},
            )
        )
    )
    assert response["uid"] == "archive-user"
    assert response["total_count"] == 1
    assert response["returned_count"] == 1
    assert response["items"][0]["memory_id"] == "visible"
    assert response["items"][0]["tier"] == "archive"
    assert response["items"][0]["agent_use"] == "explicit_archive_memory"
    assert response["policy"]["archive_capability"] is True
    assert response["global_read_gate"]["read_decision"] == "USE_MEMORY"
    assert response["rollout"]["legacy_reads_authoritative"] is False

    foreign = asyncio.run(
        search_archive_memory(FakeRequest(env, signed_headers(secret, "other-user"), {"include_archive": "true"}))
    )
    assert foreign.status_code == 403
    assert response_json(foreign)["reason"] == "missing_memory_control"


def test_archive_search_fails_closed_on_disabled_global_gate_or_malformed_control():
    secret = "archive-secret"
    env = make_env(secret)
    seed_authority(env)
    env.APP_DB.connection.execute("UPDATE cf_memory_global_read_gate SET kill_switch_active = 1 WHERE id = 1")
    env.APP_DB.connection.commit()
    disabled = asyncio.run(search_archive_memory(FakeRequest(env, signed_headers(secret), {"include_archive": "true"})))
    assert disabled.status_code == 403
    assert response_json(disabled)["reason"] == "global_memory_read_kill_switch_active"

    env = make_env(secret)
    seed_authority(env)
    env.APP_DB.connection.execute("PRAGMA ignore_check_constraints = ON")
    env.APP_DB.connection.execute("UPDATE cf_memory_control SET archive_capability = 'yes' WHERE uid = 'archive-user'")
    env.APP_DB.connection.execute("PRAGMA ignore_check_constraints = OFF")
    env.APP_DB.connection.commit()
    malformed = asyncio.run(
        search_archive_memory(FakeRequest(env, signed_headers(secret), {"include_archive": "true"}))
    )
    assert malformed.status_code == 403
    assert response_json(malformed)["reason"] == "malformed_memory_control"
