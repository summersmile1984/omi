import asyncio
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from fair_use_enforcement import fair_use_restriction, fair_use_restriction_response  # noqa: E402


class FakeDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for name in (
            "0047_fair_use_projection.sql",
            "0048_fair_use_usage_revision.sql",
            "0049_fair_use_enforcement.sql",
        ):
            self.connection.executescript((migration_dir / name).read_text())

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

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


def environment():
    return type("Env", (), {"APP_DB": FakeDatabase()})()


def insert_state(env, *, uid="user-1", stage="none", restrict_until=None, now=2_000_000_000):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_fair_use_states (uid, stage, restrict_until, updated_at) VALUES (?, ?, ?, ?)",
        (uid, stage, restrict_until, now),
    )
    env.APP_DB.connection.commit()


def insert_usage(env, speech_ms, *, uid="user-1", now=2_000_000_000):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_fair_use_usage_sources "
        "(uid, source_kind, source_id, occurred_at, speech_ms, dg_ms, updated_at, revision) "
        "VALUES (?, 'sync_fresh', 'source-1', ?, ?, 0, ?, 1)",
        (uid, now, speech_ms, now),
    )
    env.APP_DB.connection.commit()


def test_active_restriction_blocks_only_above_the_default_live_caps():
    now = 2_000_000_000
    env = environment()
    insert_state(env, stage="restrict", restrict_until=now + 90, now=now)
    insert_usage(env, 7_200_001, now=now)

    restriction = asyncio.run(fair_use_restriction(env, "user-1", now=now))
    assert restriction == {"reason": "fair_use_restricted", "retry_after": 90, "stage": "restrict"}
    response = fair_use_restriction_response(restriction)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "90"
    assert response.headers["x-omi-rate-limit-reason"] == "fair_use"

    env.APP_DB.connection.execute("UPDATE cf_fair_use_usage_sources SET speech_ms = 7200000")
    env.APP_DB.connection.commit()
    assert asyncio.run(fair_use_restriction(env, "user-1", now=now)) is None


def test_expired_restriction_is_persistently_downgraded_to_throttle():
    now = 2_000_000_000
    env = environment()
    insert_state(env, stage="restrict", restrict_until=now - 1, now=now)
    insert_usage(env, 8_000_000, now=now)

    assert asyncio.run(fair_use_restriction(env, "user-1", now=now)) is None
    row = env.APP_DB.connection.execute(
        "SELECT stage, restrict_until FROM cf_fair_use_states WHERE uid = 'user-1'"
    ).fetchone()
    assert tuple(row) == ("throttle", None)


def test_daily_audio_ceiling_blocks_every_stage():
    now = 2_000_000_000
    env = environment()
    insert_usage(env, 108_000_000, now=now)

    restriction = asyncio.run(fair_use_restriction(env, "user-1", now=now))
    assert restriction is not None
    assert restriction["reason"] == "daily_audio_ceiling"
    assert restriction["stage"] == "none"
