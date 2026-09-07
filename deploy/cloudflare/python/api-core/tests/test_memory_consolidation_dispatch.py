"""Durable dispatch through native intake, real SQL and the actual leased runner.

Only provider output and the clock are controlled. These tests drive processor
invocations; the separate Jobs tests cover Queue handoff, not hosted delivery.
"""

import asyncio
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
import sys

from fastapi import FastAPI
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

import memory_consolidation_dispatch as dispatch
import memory_consolidation_leases as leases
import memory_consolidation_runner as runner
from internal_auth import create_request_context
from memory_apply_intake import load_memory_control
from memory_apply_item import read_item
from memory_consolidation_routes import router, PROCESSOR_PATH
from test_memory_consolidation_apply import decision, environment
from test_memory_consolidation_context import services
from test_memory_mutation_lock import target


def state(db):
    row = db.connection.execute('SELECT * FROM cf_memory_consolidation_dispatch WHERE uid=?', ('owner',)).fetchone()
    return dict(row) if row else None


def drive(env):
    return asyncio.run(dispatch.process_consolidation_dispatch(env, 'owner', 0))


def provider(db, hook=None):
    env = services(db)
    original = env.AI.run
    calls = []

    async def run(model, payload):
        if 'messages' in payload:
            text = payload['messages'][-1]['content']
            memories = json.loads(text[text.index('{') :])['memories']
            ids = [row['memory_id'] for row in memories]
            calls.append(ids)
            if hook:
                await hook(ids)
            # Reject routes avoid injecting ANN publication completion into these
            # scheduler tests. Original normalization/parser/apply still execute.
            env.AI.output = {
                'decisions': [decision(read_item(db.row(key)), 'reject').model_dump(mode='json') for key in ids]
            }
        return await original(model, payload)

    env.AI.run = run
    env.model_batches = calls
    return env


def clock(db, monkeypatch):
    now = [int(datetime.now(timezone.utc).timestamp())]
    monkeypatch.setattr(dispatch, 'clock', lambda: now[0])
    instant = lambda value=None: value or datetime.fromtimestamp(now[0], timezone.utc)
    monkeypatch.setattr(leases, 'instant', instant)
    monkeypatch.setattr(runner, 'instant', instant)
    db.connection.create_function('unixepoch', 0, lambda: now[0])
    return now


def test_multiple_pages_resume_and_backdated_intake_is_not_acknowledged_by_old_cycle(target):
    db, _, create = target
    keys = [create(content=f'Fictional example {i}') for i in range(21)]
    env = provider(db)
    original_sequence = state(db)['wake_sequence']
    assert drive(env)['pending']
    first = state(db)
    assert first['cursor_memory_id'] and first['cycle_sequence'] == original_sequence
    assert len(env.model_batches) == 1 and len(env.model_batches[0]) == 20
    # Actual public intake while the old cycle is in progress, with a position
    # before its cursor (offline capture can arrive late).
    late = create(content='Late offline capture')
    db.connection.execute('UPDATE cf_memories SET captured_at=? WHERE id=?', (first['cursor_captured_at'] - 100, late))
    assert drive(env)['pending']
    assert state(db)['handled_sequence'] == original_sequence
    assert read_item(db.row(late)).processing_state.value == 'pending'
    assert drive(env)['pending']  # Apply itself records a source change.
    assert not drive(env)['pending']
    flattened = [key for group in env.model_batches for key in group]
    assert sorted(flattened) == sorted([*keys, late]) and len(flattened) == len(set(flattened))
    _, control = asyncio.run(load_memory_control(env, 'owner'))
    assert control.last_consolidation_run_at is not None
    assert state(db)['wake_sequence'] == state(db)['handled_sequence']


@pytest.mark.parametrize('malformed', ['retry', 'source'])
def test_corrupt_row_does_not_starve_healthy_memory_or_acknowledge_cycle(target, monkeypatch, malformed):
    db, _, create = target
    now = clock(db, monkeypatch)
    bad, good = create(content='Bad operational row'), create(content='Healthy source')
    db.connection.execute('UPDATE cf_memories SET captured_at=? WHERE id=?', (now[0] - 100, bad))
    env = provider(db)
    if malformed == 'retry':
        asyncio.run(leases.claim_consolidation_attempt(env, read_item(db.row(bad)), owner='fixture'))
        # Valid JSON but invalid original retry-state schema, as can occur in an
        # old operational row. No test-only runtime bypass is installed.
        db.connection.execute(
            "UPDATE cf_memory_consolidation_attempts SET state_json=json_set(state_json,'$.last_attempt_at','invalid') WHERE memory_id=?",
            (bad,),
        )
    else:
        db.connection.execute(
            "UPDATE cf_memories SET canonical_metadata_json=json_set(canonical_metadata_json,'$.ledger_sequence','invalid') WHERE id=?",
            (bad,),
        )
    bad_row = db.row(bad)
    assert drive(env)['pending']
    assert env.model_batches == [[good]]
    assert db.row(bad) == bad_row and read_item(db.row(good)).tier.value == 'archive'
    _, control = asyncio.run(load_memory_control(env, 'owner'))
    assert control.last_consolidation_run_at is None
    now[0] += 10
    assert drive(env)['pending']
    assert env.model_batches == [[good]]
    assert state(db)['handled_sequence'] == 0


def test_overlapping_delivery_runs_one_model_call(target):
    db, _, create = target
    key = create()

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause(ids):
            entered.set()
            await release.wait()

        env = provider(db, pause)
        first = asyncio.create_task(dispatch.process_consolidation_dispatch(env, 'owner', 0))
        await entered.wait()
        other = await dispatch.process_consolidation_dispatch(env, 'owner', 0)
        assert other['pending'] and 0 < other['retry_after_seconds'] <= 900
        assert env.model_batches == [[key]]
        release.set()
        await first

    asyncio.run(exercise())


@pytest.mark.parametrize('failure', ['expired', 'replaced', 'database'])
def test_failed_final_ack_cannot_move_cursor_or_watermark(target, monkeypatch, failure):
    db, _, create = target
    create()
    env = provider(db)
    before_finish = dispatch._finish

    def race():
        if failure == 'expired':
            db.connection.execute("UPDATE cf_memory_consolidation_dispatch SET lease_until=1 WHERE uid='owner'")
        elif failure == 'replaced':
            db.connection.execute(
                "UPDATE cf_memory_consolidation_dispatch SET lease_owner='new-owner' WHERE uid='owner'"
            )
        else:
            raise RuntimeError('controlled storage outage')

    async def finish(*args):
        db.before_write = race
        return await before_finish(*args)

    monkeypatch.setattr(dispatch, '_finish', finish)
    with pytest.raises((sqlite3.IntegrityError, RuntimeError)):
        drive(env)
    assert state(db)['handled_sequence'] == 0 and state(db)['cursor_memory_id'] is None
    _, control = asyncio.run(load_memory_control(env, 'owner'))
    assert control.last_consolidation_run_at is None
    if failure == 'replaced':
        assert state(db)['lease_owner'] == 'new-owner'
    assert db.connection.execute('SELECT count(*) FROM cf_memory_apply_guard').fetchone()[0] == 0


def test_legacy_principal_is_seeded_and_materialized_without_fake_business_commit():
    from test_memory_mutation_lock import Database

    db = Database()
    try:
        # Undo only the new migration in this isolated database, insert a
        # pre-journal physical row, then apply 0181 as a deployment would.
        for name in (
            'cf_memory_consolidation_wake_insert',
            'cf_memory_consolidation_wake_update',
            'cf_memory_consolidation_wake_delete',
            'cf_memory_consolidation_dispatch_admission',
        ):
            db.connection.execute('DROP TRIGGER ' + name)
        db.connection.execute('DROP TABLE cf_memory_consolidation_dispatch')
        db.connection.execute('DROP INDEX cf_memory_consolidation_scan')
        db.connection.execute('ALTER TABLE cf_memory_apply_guard DROP COLUMN consolidation_dispatch_token')
        now = int(datetime.now(timezone.utc).timestamp())
        db.connection.execute(
            "INSERT INTO cf_memories(uid,id,content,category,memory_tier,valid_at,created_at,updated_at,captured_at,processing_state) VALUES ('owner','legacy','Old capture','manual','short_term',?,?,?,?, 'processed')",
            (now, now, now, now),
        )
        migration = Path(__file__).parents[3] / 'migrations/app/0181_memory_consolidation_dispatch.sql'
        db.connection.executescript(migration.read_text())
        assert state(db)['wake_sequence'] == 1
        env = environment(db)
        claimed = asyncio.run(dispatch._claim(env, 'owner', 0, now))
        asyncio.run(dispatch._materialize_control(env, claimed))
        prior, control = asyncio.run(load_memory_control(env, 'owner'))
        assert prior and control.head_commit_id.startswith('genesis_')
        assert db.connection.execute('SELECT count(*) FROM cf_memory_commits').fetchone()[0] == 0
        assert db.connection.execute('SELECT count(*) FROM cf_memory_operations').fetchone()[0] == 0
    finally:
        db.connection.close()


def test_internal_route_binds_authority_method_path_audience_and_body(target):
    db, _, _ = target
    env = environment(db)
    env.INTERNAL_ASSERTION_SECRET = 'dispatch-test-secret'
    app = FastAPI()
    app.include_router(router)

    @app.middleware('http')
    async def attach(request, call_next):
        request.scope['env'] = env
        return await call_next(request)

    async def request(body, **binding):
        fields = dict(
            audience='api-core', method='POST', path=PROCESSOR_PATH, authority='internal', request_id='dispatch-test'
        )
        fields.update(binding)
        value, signature = create_request_context('owner', env.INTERNAL_ASSERTION_SECRET, **fields)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://core.test') as client:
            return await client.post(
                PROCESSOR_PATH, json=body, headers={'x-omi-auth-context': value, 'x-omi-internal-signature': signature}
            )

    assert asyncio.run(request({'account_generation': 0})).json() == {'pending': False, 'retry_after_seconds': 0}
    for binding in ({'authority': 'better-auth'}, {'audience': 'jobs'}, {'method': 'GET'}, {'path': '/other'}):
        assert asyncio.run(request({'account_generation': 0}, **binding)).status_code == 401
    for body in (
        {'account_generation': True},
        {'account_generation': -1},
        {'account_generation': 0, 'uid': 'victim'},
        {},
    ):
        assert asyncio.run(request(body)).status_code == 400
