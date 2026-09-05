"""Upstream users.py/notifications.py recap contracts through real HTTP + SQLite."""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
import pytz

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from daily_summary_generation import local_day_bounds  # noqa: E402
from daily_summary_routes import router  # noqa: E402
from test_daily_summary_routes import signed_headers  # noqa: E402

UID, SECRET, DAY = "recap-owner", "recap-secret", "2026-08-20"
START = int(datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp())


class Database:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        for migration in sorted((Path(__file__).parents[3] / "migrations/app").glob("*.sql")):
            self.db.executescript(migration.read_text())

    def prepare(self, sql):
        db = self.db

        class Statement:
            args = ()

            def bind(self, *args):
                self.args = args
                return self

            def execute(self):
                cursor = db.execute(sql, self.args)
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                return {"results": rows, "meta": {"changes": cursor.rowcount}}

            async def first(self):
                result = self.execute()["results"]
                return result[0] if result else None

            async def all(self):
                return self.execute()

            async def run(self):
                return self.execute()

        return Statement()

    async def batch(self, statements):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            results = [statement.execute() for statement in statements]
            self.db.commit()
            return results
        except Exception:
            self.db.rollback()
            raise


class Provider:
    def __init__(self):
        self.calls = []
        self.failure = False
        self.malformed = False
        self.entered = None
        self.resume = None

    async def run(self, model, payload):
        self.calls.append(payload)
        if self.entered:
            self.entered.set()
            await self.resume.wait()
        if self.failure:
            raise RuntimeError("provider unavailable")
        return {
            "response": (
                "bad-json"
                if self.malformed
                else json.dumps(
                    {
                        "headline": "A concrete plan",
                        "overview": "You agreed to ship the release.",
                        "day_emoji": "📦",
                        "highlights": [
                            {"topic": "Release", "summary": "You agreed to ship.", "conversation_numbers": [1, 999]}
                        ],
                        "decisions_made": [{"decision": "Ship the release", "conversation_number": 1}],
                    }
                )
            ),
            "usage": {"prompt_tokens": 123, "completion_tokens": 45},
        }


@pytest.fixture
def target():
    database, provider = Database(), Provider()
    env = SimpleNamespace(APP_DB=database, AI=provider, INTERNAL_ASSERTION_SECRET=SECRET)
    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def environment(request, call_next):
        request.scope["env"] = env
        return await call_next(request)

    async def request(method="POST", path="/v1/users/daily-summaries", *, body=None, uid=UID):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://core.test") as client:
            return await client.request(
                method,
                path,
                json=body if body is not None else {"date": DAY},
                headers=signed_headers(SECRET, uid) if uid else {},
            )

    yield database.db, provider, request
    database.db.close()


def seed_conversation(
    db,
    *,
    identifier="c1",
    uid=UID,
    started=START + 100,
    overview="We agreed to ship the release.",
    locked=0,
    transcript=True,
):
    db.execute(
        "INSERT INTO cf_conversations (uid,id,created_at,started_at,finished_at,structured_json,transcript_segments_json,is_locked) VALUES (?,?,?,?,?,?,?,?)",
        (
            uid,
            identifier,
            started,
            started,
            started + 180,
            json.dumps({"title": "Release", "overview": overview}),
            json.dumps([{"text": "Ship the release"}]) if transcript else "[]",
            locked,
        ),
    )


def seed_memory(db, identifier, *, conversation="c1", evidence="[]", **fields):
    row = {
        "uid": UID,
        "id": identifier,
        "content": "An actual stored preference",
        "category": "interesting",
        "memory_tier": "long_term",
        "valid_at": START,
        "created_at": START,
        "updated_at": START,
        "conversation_id": conversation,
        "evidence_json": evidence,
        **fields,
    }
    db.execute(f"INSERT INTO cf_memories ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))


def test_real_generation_preserves_sources_stats_language_and_idempotency(target):
    db, ai, request = target
    seed_conversation(db)
    seed_conversation(db, identifier="other", uid="someone-else")
    seed_conversation(db, identifier="locked", locked=1)
    db.execute(
        "INSERT INTO cf_action_items(uid,id,description,status,conversation_id,created_at,updated_at) VALUES (?,?,?,'active',?,?,?)",
        (UID, "t1", "Actually stored task", "c1", START, START),
    )
    seed_memory(db, "m1", conversation="other-day", evidence='[{"source_type":"conversation","source_id":"c1"}]')
    seed_memory(db, "rejected", user_review=0)
    seed_memory(db, "restricted", sensitivity_labels_json='["credential"]')
    seed_memory(db, "wrong-day", conversation="wrong-day")
    seed_memory(db, "expired", expires_at=1)
    db.execute(
        "INSERT INTO cf_desktop_daily_usage VALUES (?,?,?,?,?,?,?,?,?,?)",
        (UID, DAY, "UTC", "desktop", 120, 60, 3, 1, 1, START),
    )
    db.execute(
        "INSERT INTO cf_user_transcription_preferences(uid,language,created_at,updated_at) VALUES (?,?,?,?)",
        (UID, "zh", START, START),
    )

    async def scenario():
        result = await request()
        assert result.status_code == 200, result.text
        value = result.json()
        assert value["headline"] == "A concrete plan"
        assert value["highlights"][0]["conversation_ids"] == ["c1"]
        assert value["action_items"][0]["description"] == "Actually stored task"
        assert [ref["memory_id"] for ref in value["memories_learned"]] == ["m1"]
        assert value["stats"] == {
            "total_conversations": 1,
            "total_duration_minutes": 3,
            "action_items_count": 1,
            "memories_created": 5,
            "action_items_created": 1,
            "watching_minutes": 2,
            "proactive_moments": 3,
        }
        assert (await request()).json() == value and len(ai.calls) == 1
        assert json.loads(ai.calls[0]["messages"][1]["content"])["output_language"] == "zh"
        assert (await request("GET", f"/v1/users/daily-summaries/{value['id']}", uid="someone-else")).status_code == 404
        usage = db.execute("SELECT input_tokens,output_tokens,call_count FROM cf_llm_usage_daily").fetchone()
        assert tuple(usage) == (123, 45, 1)

    asyncio.run(scenario())


def test_timezone_windows_include_dst_and_zero_coordinates(target):
    db, ai, request = target
    ny = pytz.timezone("America/New_York")
    assert (
        local_day_bounds(datetime(2026, 3, 8).date(), ny)[1] - local_day_bounds(datetime(2026, 3, 8).date(), ny)[0]
        == 23 * 3600
    )
    assert (
        local_day_bounds(datetime(2026, 11, 1).date(), ny)[1] - local_day_bounds(datetime(2026, 11, 1).date(), ny)[0]
        == 25 * 3600
    )
    db.execute(
        "INSERT INTO cf_user_fcm_tokens VALUES (?,?,?,?,?,?)",
        (UID, "device", "fixture", "America/New_York", START, START),
    )
    seed_conversation(db, identifier="before-midnight", started=START + 4 * 3600 - 1)
    seed_conversation(db, started=START + 4 * 3600)
    db.execute(
        "UPDATE cf_conversations SET geolocation_json = ? WHERE id='c1'",
        ('{"latitude":0,"longitude":0,"address":"Origin"}',),
    )
    result = asyncio.run(request())
    assert result.status_code == 200, result.text
    assert result.json()["stats"]["total_conversations"] == 1
    assert result.json()["locations"][0]["time"] == "00:00"
    assert result.json()["locations"][0]["longitude"] == 0


def test_empty_content_releases_day_without_cooldown_and_invalid_requests_do_not_infer(target):
    db, ai, request = target

    async def scenario():
        assert (await request(uid=None)).status_code == 401
        assert (await request(body={"date": "invalid"})).status_code == 422
        assert (await request(body={"date": "2999-01-01"})).status_code == 422
        assert (await request()).status_code == 400
        seed_conversation(db, overview="")
        assert (await request()).status_code == 400
        assert len(ai.calls) == 0
        db.execute("UPDATE cf_conversations SET structured_json = ?", ('{"overview":"A useful summary"}',))
        assert (await request()).status_code == 200

    asyncio.run(scenario())


def test_concurrent_generation_has_one_owner_and_provider_failure_can_retry(target):
    db, ai, request = target
    seed_conversation(db)

    async def scenario():
        ai.entered, ai.resume = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(request())
        await asyncio.wait_for(ai.entered.wait(), 2)
        try:
            assert (await request()).status_code == 409
            assert len(ai.calls) == 1
            ai.failure = True
        finally:
            ai.resume.set()
        assert (await first).status_code == 503
        assert db.execute("SELECT COUNT(*) FROM cf_daily_summaries").fetchone()[0] == 0
        ai.failure, ai.entered = False, None
        assert (await request()).status_code == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["conversation", "memory", "task", "account"])
def test_late_source_privacy_mutation_cannot_publish_stale_recap(target, mutation):
    db, ai, request = target
    seed_conversation(db)
    seed_memory(db, "m1")
    db.execute(
        "INSERT INTO cf_action_items(uid,id,description,status,conversation_id,created_at,updated_at) VALUES (?,?,?,'active',?,?,?)",
        (UID, "t1", "Task", "c1", START, START),
    )

    async def scenario():
        ai.entered, ai.resume = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(request())
        await asyncio.wait_for(ai.entered.wait(), 2)
        try:
            if mutation == "conversation":
                db.execute("UPDATE cf_conversations SET is_locked=1")
            if mutation == "memory":
                db.execute("UPDATE cf_memories SET user_review=0")
            if mutation == "task":
                db.execute("UPDATE cf_action_items SET deleted=1")
            if mutation == "account":
                db.execute("INSERT INTO cf_account_deletion_tombstones VALUES (?,?,?)", (UID, 1, 9999999999))
        finally:
            ai.resume.set()
        result = await task
        assert result.status_code == (503 if mutation == "account" else 409), result.text
        assert db.execute("SELECT COUNT(*) FROM cf_daily_summaries").fetchone()[0] == 0

    asyncio.run(scenario())


def test_regeneration_preserves_id_visibility_creation_and_delete_revokes_writer(target):
    db, ai, request = target
    seed_conversation(db)

    async def scenario():
        original = (await request()).json()
        identifier = original["id"]
        db.execute("UPDATE cf_daily_summaries SET visibility='shared'")
        prior_cooldown = db.execute("SELECT create_until FROM cf_daily_summary_generation").fetchone()[0]
        path = f"/v1/users/daily-summaries/{identifier}"
        changed = await request(path=path + "/regenerate")
        assert changed.status_code == 200, changed.text
        assert changed.json()["id"] == identifier and changed.json()["created_at"] == original["created_at"]
        assert "regenerated_at" in changed.json()
        assert db.execute("SELECT create_until FROM cf_daily_summary_generation").fetchone()[0] == prior_cooldown
        assert db.execute("SELECT visibility FROM cf_daily_summaries").fetchone()[0] == "shared"
        assert (await request(path=path + "/regenerate")).status_code == 429
        db.execute("UPDATE cf_daily_summary_generation SET regen_until=0")
        ai.entered, ai.resume = asyncio.Event(), asyncio.Event()
        pending = asyncio.create_task(request(path=path + "/regenerate"))
        await asyncio.wait_for(ai.entered.wait(), 2)
        try:
            assert (await request("DELETE", path)).status_code == 200
        finally:
            ai.resume.set()
        assert (await pending).status_code == 409
        assert db.execute("SELECT COUNT(*) FROM cf_daily_summaries").fetchone()[0] == 0
        assert (await request()).status_code == 429

    asyncio.run(scenario())


def test_free_quota_denies_before_existing_reuse_and_malformed_prose_falls_back(target, monkeypatch, capsys):
    db, ai, request = target
    seed_conversation(db)

    async def scenario():
        ai.malformed = True
        result = await request()
        assert result.status_code == 200, result.text
        assert result.json()["stats"]["total_conversations"] == 1
        assert "malformed_doc" in capsys.readouterr().out

        async def exhausted(*args, **kwargs):
            return {"plan_type": "basic", "allowed": False, "used": 30, "limit": 30}

        monkeypatch.setattr("daily_summary_generation.chat_quota_snapshot", exhausted)
        denied = await request()
        assert denied.status_code == 402 and denied.json()["detail"]["error"] == "quota_exceeded"
        assert len(ai.calls) == 1

    asyncio.run(scenario())
