"""Native intake → owned attempts → original parser/apply → public review.

Provider failures are controlled; SQL and upstream decision policy execute.
The transaction-body oracle is test-only, never a runtime Firestore facade.
"""

import ast
import asyncio
import __future__
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
import memory_kernel_consolidation as policy
import memory_consolidation_runner as runner
from memory_apply_item import read_item
from memory_apply_intake import load_memory_control
from memory_consolidation_apply import apply_consolidation_batch
from memory_consolidation_leases import (
    ConsolidationLeaseChanged,
    claim_consolidation_attempt,
    instant,
    read_retry_state,
    release_consolidation_attempt,
)
from memory_consolidation_llm import DEFAULT_CONSOLIDATION_MODEL
from test_memory_consolidation_apply import context, decision, environment
from test_memory_consolidation_context import services, long_term, project
from test_memory_mutation_lock import target
from test_memory_review_routes import journal


def run(env, ids):
    # Repeated delivery identity deliberately does not imply lease ownership.
    return asyncio.run(runner.run_consolidation_batch(env, 'owner', ids, run_id='repeated-delivery'))


def state(database, key):
    row = database.connection.execute(
        'SELECT state_json FROM cf_memory_consolidation_attempts WHERE memory_id=? ORDER BY source_item_revision DESC',
        (key,),
    ).fetchone()
    return policy.ConsolidationRetryState.model_validate_json(row[0]) if row else None


def models(env):
    return [payload for model, payload in env.AI.calls if model == DEFAULT_CONSOLIDATION_MODEL]


def oracle():
    path = Path(__file__).parents[5] / 'backend/utils/memory/canonical_consolidation.py'
    names = {
        '_claim_retry_state_transaction',
        '_transition_retry_state_transaction',
        '_release_deferred_retry_state_transaction',
    }
    nodes = [node for node in ast.parse(path.read_text()).body if getattr(node, 'name', '') in names]
    assert len(nodes) == len(names)
    for node in nodes:
        node.decorator_list = []
    storage = SimpleNamespace(payload=None)
    ref = SimpleNamespace(get=lambda transaction=None: storage.payload)
    db = SimpleNamespace(document=lambda path: ref)
    txn = SimpleNamespace(set=lambda ref, payload: setattr(storage, 'payload', payload))
    scope = {
        **vars(policy),
        '_retry_state_document_path': lambda uid, item: 'oracle',
        '_snapshot_payload': lambda snapshot: snapshot,
    }
    exec(
        compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec', flags=__future__.annotations.compiler_flag),
        scope,
    )
    return storage, db, txn, scope


def test_claim_failure_and_deferral_match_original_transaction_bodies(target):
    database, _, create = target
    key = create(content='A native pending preference')
    item, env, now = read_item(database.row(key)), environment(database), instant()
    storage, db, txn, upstream = oracle()

    def claim(owner, time=now):
        expected, ok = upstream['_claim_retry_state_transaction'](txn, db, 'owner', item, owner, time, 600)
        actual, got = asyncio.run(claim_consolidation_attempt(env, item, owner=owner, now=time))
        assert (got, actual.state) == (ok, expected)
        return actual

    first = claim('first')
    claim('other')
    claim('first')
    expected = upstream['_release_deferred_retry_state_transaction'](
        txn, db, 'owner', item, 'index_pending', now, 'first'
    )
    actual = asyncio.run(release_consolidation_attempt(env, first, error='index_pending', now=now, deferred=True))
    assert actual == expected and actual.attempt_count == 0
    # Cloudflare adds a durable due time; original state content is unchanged.
    assert not asyncio.run(claim_consolidation_attempt(env, item, owner='early', now=now))[1]
    later = now + timedelta(seconds=5)
    database.connection.create_function('unixepoch', 0, lambda: int(later.timestamp()))
    second = claim('second', later)
    expected = upstream['_transition_retry_state_transaction'](
        txn, db, 'owner', item, 'retryable', 'output_invalid:exact_duplicate_create', later, 'second'
    )
    actual = asyncio.run(
        release_consolidation_attempt(env, second, error='output_invalid:exact_duplicate_create', now=later)
    )
    assert actual == expected and actual.attempt_count == 1


def test_same_bad_output_three_times_ends_in_original_review_and_no_fourth_model_call(target):
    database, request, create = target
    text = 'User drinks jasmine tea every morning as a long-term stable daily habit.'
    old = long_term(database, create, text)
    project(database, old, 'a' * 64)
    key = create(content=text, subject_entity_id='user', subject_attribution='user')
    item = read_item(database.row(key))
    # Retained hosted 2026-09-07 failure: create despite a score-1 exact candidate.
    bad = decision(item, memory_text=text, reconciliation='create')
    env = services(database, output={'decisions': [bad.model_dump(mode='json')]})
    env.MEMORY_VECTORS.matches = [{'id': 'a' * 64, 'score': 1.0}]
    for attempt in (1, 2, 3):
        result = run(env, [key])
        assert len(models(env)) == attempt
        assert state(database, key).attempt_count == attempt
        assert result.outcomes[key] == ('terminal_review' if attempt == 3 else 'retryable')
        assert 'output_invalid:exact_duplicate_create' in result.errors
        assert state(database, key).last_error_code == 'output_invalid:exact_duplicate_create'
        if attempt < 3:
            assert read_item(database.row(key)) == item
    assert state(database, key).status == 'terminal_review'
    assert state(database, key).lease_owner is None
    assert read_item(database.row(key)).promotion['route'] == 'review'
    reviews = request('GET', '/v3/memories/review-queue').json()
    assert len(reviews) == 1 and reviews[0]['source_short_term_id'] == key
    assert [row['id'] for row in request('GET', '/v3/memories').json()] == [old]
    result = run(env, [key])
    assert result.outcomes[key] == 'not_pending' and len(models(env)) == 3
    assert database.connection.execute('SELECT count(*) FROM cf_memory_apply_guard').fetchone()[0] == 0


def test_index_wait_refunds_attempt_and_resumes_after_durable_due_time(target, monkeypatch):
    database, _, create = target
    old = long_term(database, create, 'Prefers tea')
    project(database, old, 'a' * 64)
    key = create(content='Prefers tea')
    item = read_item(database.row(key))
    output = decision(item, 'archive', reconciliation='duplicate', target_memory_id=old)
    env = services(database, output={'decisions': [output.model_dump(mode='json')]})
    env.MEMORY_VECTORS.visible = False
    env.MEMORY_VECTORS.matches = [{'id': 'a' * 64, 'score': 1.0}]
    now = instant()
    import memory_consolidation_leases as leases

    monkeypatch.setattr(leases, 'instant', lambda value=None: value or now)
    monkeypatch.setattr(runner, 'instant', lambda: now)
    database.connection.create_function('unixepoch', 0, lambda: int(now.timestamp()))
    before = journal(database)
    assert run(env, [key]).outcomes[key] == 'deferred'
    assert state(database, key).attempt_count == 0 and env.AI.calls == []
    assert journal(database) == before
    env.MEMORY_VECTORS.visible = True
    assert run(env, [key]).outcomes[key] == 'busy'
    assert env.AI.calls == []
    now += timedelta(seconds=5)
    assert run(env, [key]).outcomes[key] == 'applied'
    assert state(database, key) is None and len(models(env)) == 1


@pytest.mark.parametrize('takeover', [False, True])
def test_expired_or_replaced_lease_cannot_apply_or_refund_at_write_boundary(target, takeover):
    database, _, create = target
    key = create(content='Prefers jasmine tea')
    snapshot, env, now = context(database, key), environment(database), instant()
    lease, claimed = asyncio.run(claim_consolidation_attempt(env, snapshot.pending_items[0], owner='old', now=now))
    assert claimed
    before = journal(database)

    # The real D1 transaction runs this after hydration and preflight checks.
    # Execute takeover synchronously here because this hook runs inside apply's loop.
    def at_write():
        later = now + timedelta(seconds=601)
        database.connection.create_function('unixepoch', 0, lambda: int(later.timestamp()))
        if takeover:
            replacement = lease.state.model_copy(
                update={
                    'lease_owner': 'new',
                    'attempt_count': 2,
                    'last_attempt_at': later,
                    'lease_expires_at': later + timedelta(seconds=600),
                }
            )
            database.connection.execute(
                'UPDATE cf_memory_consolidation_attempts SET state_json=?,lease_until=? WHERE memory_id=?',
                (replacement.model_dump_json(), int(replacement.lease_expires_at.timestamp()), key),
            )

    database.before_write = at_write
    with pytest.raises(sqlite3.IntegrityError, match='memory_consolidation_lease_changed'):
        asyncio.run(
            apply_consolidation_batch(
                env,
                snapshot,
                policy.ConsolidationAgentBatch(decisions=[decision(snapshot.pending_items[0])]),
                run_id='stale',
                now=now,
                leases=(lease,),
            )
        )
    assert journal(database) == before and read_item(database.row(key)) == snapshot.pending_items[0]
    with pytest.raises(ConsolidationLeaseChanged):
        asyncio.run(release_consolidation_attempt(env, lease, error='late', now=now))


def test_abandoned_third_attempt_is_settled_without_another_inference(target):
    database, request, create = target
    key = create(content='A pending source')
    env, item = services(database), read_item(database.row(key))
    for attempt in (1, 2, 3):
        lease, claimed = asyncio.run(claim_consolidation_attempt(env, item, owner=str(attempt)))
        assert claimed and lease.state.attempt_count == attempt
        if attempt < 3:
            asyncio.run(release_consolidation_attempt(env, lease, error='parse_failed:invalid_json'))
    expired = lease.state.model_copy(update={'lease_expires_at': instant() - timedelta(seconds=1)})
    database.connection.execute(
        'UPDATE cf_memory_consolidation_attempts SET state_json=?,lease_until=? WHERE memory_id=?',
        (expired.model_dump_json(), int(expired.lease_expires_at.timestamp()), key),
    )
    assert run(env, [key]).outcomes[key] == 'terminal_review'
    assert env.AI.calls == [] and state(database, key).attempt_count == 3
    assert len(request('GET', '/v3/memories/review-queue').json()) == 1


@pytest.mark.parametrize('failures', [1, 2])
def test_review_write_failure_uses_original_quarantine_or_retains_exhausted_work(target, monkeypatch, failures):
    database, _, create = target
    key = create(content='A pending source')
    env, item = services(database, output={'decisions': []}), read_item(database.row(key))
    for _ in range(2):
        assert run(env, [key]).outcomes[key] == 'retryable'
    actual = runner.apply_consolidation_batch
    calls = []

    async def faulty(*args, **kwargs):
        calls.append(kwargs['terminal_status'])
        if len(calls) <= failures:
            # Fail the actual SQL transaction, not the terminal planner.
            database.connection.execute(
                "CREATE TEMP TRIGGER test_review_outage BEFORE INSERT ON cf_memory_apply_guard "
                "BEGIN SELECT RAISE(ABORT,'controlled write outage'); END"
            )
        try:
            return await actual(*args, **kwargs)
        finally:
            database.connection.execute('DROP TRIGGER IF EXISTS test_review_outage')

    monkeypatch.setattr(runner, 'apply_consolidation_batch', faulty)
    result = run(env, [key])
    assert calls == ['terminal_review', 'quarantined'] and len(models(env)) == 3
    if failures == 1:
        assert result.outcomes[key] == 'quarantined' and state(database, key).status == 'quarantined'
        current = read_item(database.row(key))
        assert current.tier.value == 'short_term' and current.promotion['processing_status'] == 'processing_blocked'
        assert run(env, [key]).outcomes[key] == 'not_pending'
        assert len(models(env)) == 3
    else:
        assert result.outcomes[key] == 'retryable' and state(database, key).attempt_count == 3
        assert read_item(database.row(key)) == item
        monkeypatch.setattr(runner, 'apply_consolidation_batch', actual)
        assert run(env, [key]).outcomes[key] == 'terminal_review'
        assert len(models(env)) == 3


def test_retry_batch_is_isolated_and_healthy_remaining_work_can_complete(target):
    database, _, create = target
    poison = create(content='An invalid source output')
    healthy = create(content='An unrelated valid source')
    env = services(database, output={'decisions': []})
    assert run(env, [poison]).outcomes[poison] == 'retryable'
    result = run(env, [poison, healthy])
    assert result.selected == (poison,) and result.remaining == (healthy,)
    env.AI.output = {'decisions': [decision(read_item(database.row(healthy)), 'archive').model_dump(mode='json')]}
    result = run(env, result.remaining)
    assert result.outcomes[healthy] == 'applied'
    assert state(database, healthy) is None and state(database, poison).attempt_count == 2


def test_source_edit_and_deletion_invalidate_old_claims_and_purge_operational_state(target):
    database, request, create = target
    key = create(content='The original version')
    env, item = environment(database), read_item(database.row(key))
    old, ok = asyncio.run(claim_consolidation_attempt(env, item, owner='old'))
    assert ok
    assert request('PATCH', '/v3/memories/' + key, body={'value': 'The corrected version'}).status_code == 200
    changed = read_item(database.row(key))
    new, ok = asyncio.run(claim_consolidation_attempt(env, changed, owner='new'))
    assert ok and new.state.attempt_count == 1 and new.retry_id != old.retry_id
    _, control = asyncio.run(load_memory_control(env, 'owner'))
    with pytest.raises(ConsolidationLeaseChanged):
        old.validate(changed, control)
    database.connection.execute('DELETE FROM cf_memories WHERE id=?', (key,))
    assert state(database, key) is None


def test_generation_fence_invalidates_old_lease_without_resetting_source_retry_budget(target):
    from memory_apply_intake import control_statement

    database, _, create = target
    key = create(content='A pending source')
    item, env = read_item(database.row(key)), environment(database)
    old, ok = asyncio.run(claim_consolidation_attempt(env, item, owner='old'))
    assert ok
    _, control = asyncio.run(load_memory_control(env, 'owner'))
    changed = control.model_copy(update={'source_generation': control.source_generation + 1})
    asyncio.run(control_statement(env.APP_DB, 'owner', changed).run())
    with pytest.raises(ConsolidationLeaseChanged):
        asyncio.run(release_consolidation_attempt(env, old, error='late', deferred=True))
    new, ok = asyncio.run(claim_consolidation_attempt(env, item, owner='new'))
    assert ok and new.state.attempt_count == 2 and new.source_generation == changed.source_generation
    assert database.connection.execute('SELECT count(*) FROM cf_memory_consolidation_attempts').fetchone()[0] == 1
    with pytest.raises(ConsolidationLeaseChanged):
        old.validate(item, changed)
