"""Canonical Candidate idempotency/coalescing through actual App migration SQL."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from candidate_create import create_candidate, get_candidate
from candidate_db import CandidateTransaction
from candidate_kernel_models import CandidateCreate, CandidateStatus
from candidate_kernel_policy import CandidateConflictError, CandidateGenerationMismatchError
from test_memory_mutation_lock import Database


@pytest.fixture
def env():
    db = Database()
    yield SimpleNamespace(APP_DB=db)
    db.connection.close()


def proposal(
    *, workstream=False, description='Complete the report', confidence=0.85, evidence='source-1', priority=None
):
    payload = {'description': description}
    if priority is not None:
        payload['priority'] = priority
    fields = dict(
        capture_confidence=confidence,
        ownership_confidence=0.85,
        evidence_refs=[{'kind': 'memory_item', 'id': evidence, 'scope': 'canonical'}],
        source_surface='memory_recurrence' if workstream else 'chat',
        proposed_action='create',
    )
    if workstream:
        fields.update(
            subject_kind='workstream',
            workstream_proposal={
                'title': 'Recurring report',
                'objective': 'Keep the report current',
                'anchor_task': payload,
            },
        )
    else:
        fields.update(subject_kind='task', task_change=payload)
    return CandidateCreate.model_validate(fields)


def create(env, value=None, *, key='request-1', generation=0, now=None):
    return asyncio.run(
        create_candidate(env, 'owner', value or proposal(), idempotency_key=key, generation=generation, now=now)
    )


def rows(env):
    return {
        table: [tuple(row) for row in env.APP_DB.connection.execute('SELECT * FROM ' + table)]
        for table in ('cf_candidates', 'cf_candidate_aliases', 'cf_candidate_claims', 'cf_candidate_write_guard')
    }


@pytest.mark.parametrize('workstream', [False, True])
def test_new_principal_retries_one_request_without_duplicate_or_cross_account_read(env, workstream):
    value = proposal(workstream=workstream)
    first = create(env, value)
    assert create(env, value) == first
    assert asyncio.run(get_candidate(env, 'other', first.candidate_id)) is None
    stored = rows(env)
    assert len(stored['cf_candidates']) == len(stored['cf_candidate_aliases']) == 1
    assert len(stored['cf_candidate_claims']) == (0 if workstream else 1)
    assert stored['cf_candidate_write_guard'] == []
    assert first.expires_at == first.created_at + timedelta(days=2)


def test_same_key_changed_proposal_conflicts_without_mutating_existing_receipt(env):
    create(env)
    before = rows(env)
    with pytest.raises(CandidateConflictError, match='different proposal'):
        create(env, proposal(description='A different action'))
    assert rows(env) == before
    with pytest.raises(CandidateGenerationMismatchError):
        create(env, generation=1)
    assert rows(env) == before


def test_semantic_task_coalescing_keeps_identity_and_merges_original_annotations(env):
    first = create(env, proposal(description='[screen] Complete the report', priority='low'))
    second = create(
        env,
        proposal(description='complete the report', confidence=0.95, evidence='source-2', priority='high'),
        key='request-2',
    )
    assert second.candidate_id == first.candidate_id
    assert second.task_change.description == first.task_change.description
    assert second.capture_confidence == 0.95 and second.task_change.priority.value == 'high'
    assert [value.id for value in second.evidence_refs] == ['source-1', 'source-2']
    assert len(rows(env)['cf_candidates']) == 1 and len(rows(env)['cf_candidate_aliases']) == 2


def test_new_request_does_not_merge_into_expired_pending_suggestion(env):
    old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    first = create(env, now=old)
    second = create(env, key='request-2', now=old + timedelta(days=3))
    assert first.candidate_id != second.candidate_id
    assert len(rows(env)['cf_candidates']) == 2
    assert create(env, now=old + timedelta(days=3)) == first


def test_late_alias_failure_rolls_back_candidate_claim_and_guard(env):
    before = rows(env)
    env.APP_DB.connection.execute(
        "CREATE TRIGGER fail_alias BEFORE INSERT ON cf_candidate_aliases BEGIN SELECT RAISE(ABORT,'alias unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match='alias unavailable'):
        create(env)
    assert rows(env) == before


def test_concurrent_coalescing_replans_against_the_new_record(env):
    first = create(env)
    newer = first.model_copy(update={'capture_confidence': 0.99})

    def race():
        db = env.APP_DB.connection
        original = db.execute(
            'SELECT record_json FROM cf_candidates WHERE candidate_id=?', (first.candidate_id,)
        ).fetchone()[0]
        import json

        db.execute(
            'INSERT INTO cf_candidate_write_guard(uid,account_generation,candidates_json) VALUES (?,?,?)',
            ('owner', 0, json.dumps([{'id': first.candidate_id, 'before': original}])),
        )
        db.execute(
            'UPDATE cf_candidates SET record_json=? WHERE candidate_id=?',
            (newer.model_dump_json(exclude_none=True), first.candidate_id),
        )
        db.execute('DELETE FROM cf_candidate_write_guard WHERE uid=?', ('owner',))

    env.APP_DB.before_write = race
    merged = create(env, proposal(confidence=0.9, evidence='source-2'), key='request-2')
    assert merged.capture_confidence == 0.99
    assert [value.id for value in merged.evidence_refs] == ['source-1', 'source-2']
    assert len(rows(env)['cf_candidates']) == 1
    assert rows(env)['cf_candidate_write_guard'] == []


def test_new_capture_reuses_active_accepted_task_and_updates_provenance(env):
    first = create(env)
    now = datetime.now(timezone.utc)
    accepted = first.model_copy(
        update={
            'status': CandidateStatus.accepted,
            'result_task_id': 'task-fixture',
            'resolution_reason': 'accepted',
            'resolved_at': now,
            'expires_at': None,
        }
    )

    async def accept_fixture():
        tx = CandidateTransaction(env, 'owner', 0)
        await tx.record('candidates', first.candidate_id)
        tx.put('candidates', first.candidate_id, accepted.model_dump(mode='json', exclude_none=True))
        tx.statements.append(
            env.APP_DB.prepare(
                "INSERT INTO cf_action_items(uid,id,description,status,owner,created_at,updated_at,candidate_id) VALUES ('owner','task-fixture',?,'active','unknown',?,?,?)"
            ).bind(first.task_change.description, int(now.timestamp()), int(now.timestamp()), first.candidate_id)
        )
        await tx.commit()

    asyncio.run(accept_fixture())
    reused = create(env, proposal(evidence='source-2', confidence=0.95, priority='high'), key='request-2')
    assert reused.candidate_id == first.candidate_id and reused.status == CandidateStatus.accepted
    task = dict(env.APP_DB.connection.execute('SELECT * FROM cf_action_items').fetchone())
    assert task['capture_confidence'] == 0.95 and task['priority'] == 'high'
    assert 'source-2' in task['provenance_json']
    assert len(rows(env)['cf_candidates']) == 1


def test_mutation_storage_and_request_identity_preserve_explicit_clear(env):
    body = proposal().model_dump(mode='json')
    body.update(proposed_action='update', task_id='existing-task', task_change={'due_at': None})
    clear = CandidateCreate.model_validate(body)
    first = create(env, clear)
    loaded = asyncio.run(get_candidate(env, 'owner', first.candidate_id))
    assert loaded.task_change.model_fields_set == {'due_at'}
    assert loaded.task_change.model_dump(exclude_unset=True) == {'due_at': None}
    assert create(env, clear) == first
    # Same non-null values, different instruction: clear the date as well.
    body['task_change'] = {'priority': 'high'}
    create(env, CandidateCreate.model_validate(body), key='priority-change')
    body['task_change']['due_at'] = None
    before = rows(env)
    with pytest.raises(CandidateConflictError, match='different proposal'):
        create(env, CandidateCreate.model_validate(body), key='priority-change')
    assert rows(env) == before
