"""Real native intake/SQL/dispatch with bounded synthetic provider responses."""

import asyncio
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from memory_apply_item import read_item
from memory_consolidation_context import gather_consolidation_context
from memory_consolidation_leases import (
    ConsolidationLeaseChanged,
    claim_consolidation_attempt,
    release_consolidation_attempt,
    release_unused_consolidation_attempt,
)
from memory_consolidation_llm import ConsolidationInferenceError, MAX_INPUT_BYTES, model_messages
from memory_kernel_consolidation import build_consolidation_llm_messages
from test_memory_consolidation_context import long_term, project, services
from test_memory_consolidation_dispatch import clock, drive, provider, state as dispatch_state
from test_memory_consolidation_retry import models, run, state as retry_state
from test_memory_mutation_lock import target
from test_memory_review_routes import journal


def prompt_context(context):
    formatter = build_consolidation_llm_messages.__globals__['format_consolidation_llm_context']
    return json.loads(formatter(context))


def candidates(db, create, count, size):
    matches = []
    for number in range(count):
        key = long_term(db, create, str(number) + '😊' * size)
        vector_id = hashlib.sha256(key.encode()).hexdigest()
        project(db, key, vector_id)
        matches.append({'id': vector_id, 'score': 0.9})
    return matches


def test_oversized_unicode_batches_resume_without_trimming_context_or_spending_retries(target):
    db, request, create = target
    matches = candidates(db, create, 1, 600)
    rejected = create(content='Never claim this fictional example describes me')
    assert request('POST', '/v3/memories/' + rejected + '/review?value=false').status_code == 200
    db.connection.execute("UPDATE cf_memories SET status='hidden' WHERE id=?", (rejected,))
    keys = [create(content=str(i) + '😊' * 1999) for i in range(8)]
    env = provider(db)
    env.MEMORY_VECTORS.matches = matches
    full = asyncio.run(gather_consolidation_context(env, 'owner', keys))
    expected = prompt_context(full)
    with pytest.raises(ConsolidationInferenceError, match='input_too_large'):
        model_messages(full)
    pending = True
    for _ in range(8):
        count = len(models(env))
        pending = drive(env)['pending']
        assert len(models(env)) - count <= 1
        # Planning-only reservations do not become attempt_count=0 retries,
        # which would force all remaining work into single-source invocations.
        assert db.connection.execute('SELECT count(*) FROM cf_memory_consolidation_attempts').fetchone()[0] == 0, [
            retry_state(db, key).last_error_code for key in keys if retry_state(db, key)
        ]
        if not pending:
            break
    assert not pending
    batches = models(env)
    assert len(batches) > 1 and any(len(group) > 1 for group in env.model_batches)
    assert sorted(key for group in env.model_batches for key in group) == sorted(keys)
    expected_sources = {row['memory_id']: row for row in expected['memories']}
    expected_groups = {row['anchor_memory_id']: row for row in expected['candidate_groups']}
    for batch in batches:
        messages = batch['messages']
        assert sum(len(message['content'].encode('utf-8')) for message in messages) <= MAX_INPUT_BYTES
        assert (
            hashlib.sha256(messages[0]['content'].encode()).hexdigest()
            == 'be2a82a77a5286d46b3af5b6320ebfbcbd2eb28bbeb76f82e637085c411bd9aa'
        )
        payload = json.loads(messages[1]['content'].split('Batch JSON:\n')[1])
        assert payload['owner_rejected_examples'] == expected['owner_rejected_examples']
        for row in payload['memories']:
            assert row == expected_sources[row['memory_id']]
        for group in payload['candidate_groups']:
            assert group == expected_groups[group['anchor_memory_id']]
    assert dispatch_state(db)['wake_sequence'] == dispatch_state(db)['handled_sequence']


def test_single_source_over_budget_reaches_original_review_and_healthy_work_completes(target, monkeypatch):
    db, request, create = target
    now = clock(db, monkeypatch)
    matches = candidates(db, create, 8, 900)
    bad = create(content='😊' * 2000)
    good = create(content='Healthy small observation')
    db.connection.execute('UPDATE cf_memories SET captured_at=? WHERE id=?', (now[0] - 10, bad))
    env = provider(db)
    env.MEMORY_VECTORS.matches = matches
    for _ in range(12):
        pending = drive(env)['pending']
        if not pending:
            break
        now[0] += 10
    assert not pending
    state = retry_state(db, bad)
    assert state.attempt_count == 3 and state.status == 'terminal_review'
    assert state.last_error_code == 'input_too_large'
    assert env.model_batches == [[good]]
    assert retry_state(db, good) is None
    reviews = request('GET', '/v3/memories/review-queue').json()
    assert len(reviews) == 1 and reviews[0]['source_short_term_id'] == bad
    assert read_item(db.row(bad)).promotion['route'] == 'review'


def test_smaller_plan_rechecks_previously_excluded_processed_source_publication(target):
    db, _, create = target
    keys = [create(content=str(i) + '😊' * 1999) for i in range(6)]
    excluded = keys[-1]
    # Existing pre-normalization native row: eligible for consolidation and for
    # vector publication, but not yet published. Full-batch admission excludes
    # it; a smaller batch must now wait for it as a potential candidate.
    db.connection.execute(
        "UPDATE cf_memories SET processing_state='processed', canonical_metadata_json="
        "json_set(canonical_metadata_json,'$.promotion',json_object('intake_payload_digest',"
        "json_extract(canonical_metadata_json,'$.promotion.intake_payload_digest'))) WHERE id=?",
        (excluded,),
    )
    env = services(db)
    before = journal(db)
    result = run(env, keys)
    assert set(result.outcomes.values()) == {'deferred'}
    assert models(env) == [] and journal(db) == before
    assert all(retry_state(db, key).attempt_count == 0 for key in keys)
    outbox = db.connection.execute(
        'SELECT operation FROM cf_vector_projection_outbox WHERE source_id=?', (excluded,)
    ).fetchone()
    assert outbox['operation'] == 'upsert'


def test_unused_reservation_does_not_erase_prior_failure_budget(target):
    db, _, create = target
    key = create(content='A source with a previous failed attempt')
    env, item = services(db), read_item(db.row(key))
    first, claimed = asyncio.run(claim_consolidation_attempt(env, item, owner='first'))
    assert claimed and first.newly_created
    asyncio.run(release_consolidation_attempt(env, first, error='output_failed'))
    second, claimed = asyncio.run(claim_consolidation_attempt(env, item, owner='second'))
    assert claimed and not second.newly_created and second.state.attempt_count == 2
    asyncio.run(release_unused_consolidation_attempt(env, second))
    state = retry_state(db, key)
    assert state.attempt_count == 1 and state.last_error_code == 'output_failed'
    assert state.status == 'retryable' and state.lease_owner is None


@pytest.mark.parametrize('fault', ['expired', 'replaced', 'generation'])
def test_unused_reservation_cannot_delete_changed_ownership(target, fault):
    db, _, create = target
    key = create(content='An owned planning reservation')
    env, item = services(db), read_item(db.row(key))
    lease, claimed = asyncio.run(claim_consolidation_attempt(env, item, owner='original'))
    assert claimed and lease.newly_created

    def race():
        if fault == 'expired':
            db.connection.create_function('unixepoch', 0, lambda: int(lease.state.lease_expires_at.timestamp()) + 1)
        elif fault == 'replaced':
            changed = lease.state.model_copy(update={'lease_owner': 'replacement'})
            db.connection.execute(
                'UPDATE cf_memory_consolidation_attempts SET state_json=? WHERE memory_id=?',
                (changed.model_dump_json(), key),
            )
        else:
            db.connection.execute(
                "UPDATE cf_memory_apply_control SET control_json=json_set(control_json,'$.source_generation',source_generation+1),source_generation=source_generation+1 WHERE uid=?",
                ('owner',),
            )

    db.before_write = race
    with pytest.raises(ConsolidationLeaseChanged):
        asyncio.run(release_unused_consolidation_attempt(env, lease))
    assert retry_state(db, key).attempt_count == 1
    assert retry_state(db, key).lease_owner == ('replacement' if fault == 'replaced' else 'original')
