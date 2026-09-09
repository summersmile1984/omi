"""Canonical review source/decision contracts from the original upstream owner.

Expected policies come from canonical_memory_adapter.resolve_canonical_memory_review
and memory_apply_store's source/read/write contracts, not the retired D1 heuristic.
The consolidation decision is controlled input; public HTTP resolution is real.
"""

import ast
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import runpy
import sqlite3
import sys
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple, cast

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from memory_apply_item import read_item
from memory_apply_mutation import apply_user_memory_mutation
from memory_kernel_admission import REQUIRED_PROCESSOR_ID, REQUIRED_PROCESSOR_VERSION
from memory_kernel_item import MemoryItem, MemoryLayer
from memory_kernel_short_term_lifecycle import default_short_term_expiry
from memory_review_policy import review_resolution_patch
from test_memory_mutation_lock import target  # noqa: F401
from test_memory_privacy_delete import artifact

ROOT = Path(__file__).parents[5]


def seed_review(database, memory_id, conflicts=(), *, uid='owner', locked=False):
    """Represent a prior admitted consolidation review, not an intake heuristic."""

    async def send(_):
        pass

    env = SimpleNamespace(APP_DB=database, JOBS=SimpleNamespace(send=send))

    def patch(item, now):
        return {}, {'promotion_audit': {**(item.promotion or {}), 'route': 'review'}}, {}

    asyncio.run(
        apply_user_memory_mutation(
            env,
            uid,
            memory_id,
            int(datetime.now(timezone.utc).timestamp()),
            kind='test_prior_consolidation',
            build_patch=patch,
        )
    )
    if locked:
        database.lock(memory_id)
    item = read_item(database.row(memory_id))
    build = runpy.run_path(str(ROOT / 'backend/models/memory_review.py'))['build_memory_review_conflict']
    review = build(
        fact={'id': memory_id, 'content': item.content, 'veracity': 0.8},
        conflict_with=list(conflicts),
        authority='canonical_memory',
        source_commit_id=item.ledger_commit_id,
        source_item_revision=item.item_revision,
        source_content_hash=item.content_hash,
    )
    record = {'uid': uid}
    for key, value in review.items():
        if isinstance(value, (dict, list)):
            record[key + '_json'] = json.dumps(value)
        elif isinstance(value, datetime):
            record[key] = int(value.timestamp())
        else:
            record[key] = value
    database.connection.execute(
        'INSERT INTO cf_memory_review_queue (' + ', '.join(record) + ') VALUES (' + ','.join('?' for _ in record) + ')',
        list(record.values()),
    )
    return review


def journal(database):
    return {
        table: [dict(row) for row in database.connection.execute('SELECT * FROM ' + table)]
        for table in (
            'cf_memories',
            'cf_memory_operations',
            'cf_memory_commits',
            'cf_memory_apply_control',
            'cf_memory_outbox',
            'cf_memory_review_queue',
            'cf_memory_review_apply_guard',
            'cf_memory_privacy_deletions',
            'cf_memory_privacy_receipts',
            'cf_destructive_operation_gates',
        )
    }


def resolve(request, review, decision='accept', **values):
    return request(
        'POST', '/v3/memories/review-queue/' + review['review_id'] + '/resolve', body={'decision': decision, **values}
    )


def test_intake_cannot_replace_consolidation_with_a_structural_conflict(target):
    database, request, create = target
    for city in ['NYC', 'SF']:
        create(
            content='Lives in ' + city, predicate='resides_in', arguments={'location': city}, subject_entity_id='user'
        )
    assert request('GET', '/v3/memories/review-queue').json() == []
    assert database.connection.execute('SELECT count(*) FROM cf_memory_review_queue').fetchone()[0] == 0


def test_queue_checks_owner_and_canonical_source_instead_of_timestamp(target):
    database, request, create = target
    memory_id = create(content='A candidate')
    review = seed_review(database, memory_id)
    assert request('GET', '/v3/memories/review-queue', uid='other').json() == []
    assert request('GET', '/v3/memories/review-queue?limit=501').status_code == 400
    assert (
        request('GET', '/v3/memories/review-queue').json()[0]['source_item_revision']
        == database.row(memory_id)['item_revision']
    )
    before = database.row(memory_id)
    # Same-second edits still revoke review ownership through revision/head.
    assert request('PATCH', '/v3/memories/' + memory_id + '/baseline?value=true').status_code == 200
    assert database.row(memory_id)['item_revision'] == before['item_revision'] + 1
    response = resolve(request, review)
    assert response.status_code == 200 and response.json()['status'] == 'stale_review'
    assert response.json()['item']['candidate'] == {}
    assert request('GET', '/v3/memories/review-queue').json() == []


def test_historical_timestamp_queue_is_readable_but_cannot_mutate_a_memory(target):
    database, request, create = target
    memory_id = create()
    review = seed_review(database, memory_id)
    database.connection.execute(
        "UPDATE cf_memory_review_queue SET source_commit_id='d1-memory:legacy',source_item_revision=?",
        (database.row(memory_id)['updated_at'],),
    )
    before = database.row(memory_id)
    assert resolve(request, review).json()['status'] == 'stale_review'
    assert database.row(memory_id) == before


@pytest.mark.parametrize(
    'decision,correction',
    [
        ('accept', None),
        ('correct', {'content': 'Corrected', 'arg_changes': {'location': 'LA'}, 'target_fact_id': 'unrelated'}),
    ],
)
def test_resolution_returns_pending_short_term_and_preserves_conflicts(target, decision, correction):
    database, request, create = target
    other = create(content='Preserved conflict')
    memory_id = create(content='Original candidate', arguments={'retained': True})
    review = seed_review(database, memory_id, [other])
    prior, conflict = database.row(memory_id), database.row(other)
    before = journal(database)
    response = resolve(request, review, decision, correction=correction, reason='User review')
    assert response.status_code == 200, response.text
    body = response.json()
    item = read_item(database.row(memory_id))
    assert body['status'] == 'resolved' and body['item']['status'] == 'accepted'
    assert body['commit']['commit_id'] == item.ledger_commit_id
    assert body['correction'] is None and body['item']['candidate'] == {} and body['item']['permitted_uses'] == []
    assert body['item']['source_content_hash'] is None
    assert item.item_revision == prior['item_revision'] + 1 and item.tier == MemoryLayer.short_term
    assert item.processing_state.value == 'pending' and item.promotion['processing_status'] == 'pending_processing'
    assert item.promotion['review_decision'] == decision and 'route' not in item.promotion
    assert item.content == ('Corrected' if decision == 'correct' else prior['content'])
    if decision == 'correct':
        assert item.arguments == {'retained': True, 'location': 'LA'}
    assert database.row(other) == conflict
    after = journal(database)
    assert len(after['cf_memory_operations']) == len(before['cf_memory_operations']) + 1
    assert len(after['cf_memory_commits']) == len(before['cf_memory_commits']) + 1
    assert after['cf_memory_review_apply_guard'] == []
    assert resolve(request, review).json()['status'] == 'already_resolved'
    assert journal(database) == after


@pytest.mark.parametrize(
    'decision,correction',
    [
        ('accept', {'content': 'Not permitted'}),
        ('correct', {}),
        ('correct', {'arg_changes': []}),
        ('correct', {'content': ''}),
    ],
)
def test_invalid_resolution_has_no_writes(target, decision, correction):
    database, request, create = target
    review = seed_review(database, create())
    before = journal(database)
    assert resolve(request, review, decision, correction=correction).status_code == 400
    assert journal(database) == before


@pytest.mark.parametrize('decision', ['reject', 'timeout'])
def test_reject_and_zero_confidence_timeout_erase_candidate_even_when_locked(target, decision):
    database, request, create = target
    other = create(content='Retained conflict')
    memory_id = create(content='Private review content')
    review = seed_review(database, memory_id, [other], locked=True)
    before = database.row(other)
    response = resolve(request, review, decision, current_veracity=0.0, reason='Private reason')
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['decision'] == ('reject' if decision == 'reject' else 'drop')
    assert body['item']['candidate'] == {} and body['item']['reason'] == 'canonical_review_' + body['decision']
    assert database.row(memory_id) is None and database.row(other) == before
    assert database.connection.execute('SELECT count(*) FROM cf_memory_review_queue').fetchone()[0] == 0
    assert 'Private review content' not in json.dumps(journal(database))
    assert 'Private reason' not in json.dumps(journal(database))


def test_review_rejection_waits_for_observed_provider_erasure_on_retry(target):
    database, request, create = target
    memory_id = create()
    review = seed_review(database, memory_id)
    artifact(database, 'owner', memory_id)
    response = resolve(request, review, 'reject')
    assert response.status_code == 503 and response.json()['error'] == 'memory_cleanup_pending'
    assert database.row(memory_id)['content'] is None
    before = journal(database)
    assert resolve(request, review, 'reject').status_code == 503
    assert journal(database) == before
    database.connection.execute('DELETE FROM cf_memory_vector_artifacts')
    response = resolve(request, review, 'reject')
    assert response.status_code == 200 and response.json()['status'] == 'already_resolved'
    assert database.row(memory_id) is None
    assert database.connection.execute('SELECT count(*) FROM cf_memory_privacy_deletions').fetchone()[0] == 0


@pytest.mark.parametrize('decision', ['accept', 'reject'])
def test_late_resolution_write_failure_rolls_back_item_head_and_privacy(target, decision):
    database, request, create = target
    review = seed_review(database, create())
    database.connection.execute(
        "CREATE TRIGGER fail_review BEFORE UPDATE ON cf_memory_review_queue WHEN NEW.decision IS NOT NULL BEGIN SELECT RAISE(ABORT,'injected resolution failure'); END"
    )
    before = journal(database)
    assert resolve(request, review, decision).status_code == 503
    assert journal(database) == before


def test_queue_race_cannot_commit_a_second_decision(target):
    database, request, create = target
    review = seed_review(database, create())
    expected = {}

    def change():
        database.connection.execute("UPDATE cf_memory_review_queue SET status='accepted',decision='accept'")
        expected.update(journal(database))

    database.before_write = change
    assert resolve(request, review, 'reject').json()['status'] == 'already_resolved'
    assert journal(database) == expected


@pytest.mark.parametrize(
    'decision,correction',
    [
        ('accept', None),
        ('correct', {'arg_changes': {'city': 'SF'}}),
        ('correct', {'content': 'Updated', 'target_fact_id': 'audit-only'}),
    ],
)
def test_accept_and_correct_patch_matches_original_upstream_function(target, decision, correction):
    database, _, create = target
    memory_id = create()
    review = seed_review(database, memory_id)
    item = read_item(database.row(memory_id))
    now = datetime.now(timezone.utc)
    captured = {}

    def apply(uid, memory_id, **options):
        captured['patch'] = options['build_patch'](item, now)
        return item, item

    source = ROOT / 'backend/utils/memory/canonical_memory_adapter.py'
    module = ast.parse(source.read_text())
    names = {'resolve_canonical_memory_review', '_clear_settled_promotion_route'}
    nodes = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name in names]
    settled = next(
        n
        for n in module.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == '_SETTLED_PROMOTION_FIELDS' for t in n.targets)
    )
    client = SimpleNamespace(
        document=lambda _: SimpleNamespace(get=lambda: SimpleNamespace(exists=True, to_dict=lambda: item.model_dump()))
    )
    scope = dict(
        Any=Any,
        Dict=Dict,
        Optional=Optional,
        Tuple=Tuple,
        cast=cast,
        Payload=Dict[str, Any],
        MemoryItem=MemoryItem,
        MemoryLayer=MemoryLayer,
        datetime=datetime,
        copy=copy,
        hashlib=hashlib,
        MemoryCollections=lambda **_: SimpleNamespace(memory_items='items'),
        _snapshot_payload=lambda s: s.to_dict(),
        CanonicalReviewResolution=lambda **kw: SimpleNamespace(**kw),
        _apply_canonical_user_mutation=apply,
        CanonicalReviewResolutionConflict=RuntimeError,
        invalidate_kg_for_memory_retraction=lambda *a, **k: None,
        REQUIRED_PROMOTION_STATUS_PENDING='pending',
        REQUIRED_PROCESSING_STATUS_PENDING='pending_processing',
        REQUIRED_PROCESSOR_ID=REQUIRED_PROCESSOR_ID,
        REQUIRED_PROCESSOR_VERSION=REQUIRED_PROCESSOR_VERSION,
        default_short_term_expiry=default_short_term_expiry,
    )
    exec(compile(ast.Module(body=[settled, *nodes], type_ignores=[]), str(source), 'exec'), scope)
    scope['resolve_canonical_memory_review'](
        'owner', memory_id, review_id=review['review_id'], decision=decision, correction=correction, db_client=client
    )
    logical, patch, _ = review_resolution_patch(
        item, now, review_id=review['review_id'], decision=decision, correction=correction
    )
    assert (logical, patch) == captured['patch']


def test_review_guard_requires_the_enclosing_canonical_transaction(target):
    database, _, create = target
    review = seed_review(database, create())
    with pytest.raises(sqlite3.IntegrityError, match='memory_review_apply_required'):
        database.connection.execute(
            'INSERT INTO cf_memory_review_apply_guard(uid,review_id,memory_id,decision,source_commit_id,source_item_revision,source_content_hash) VALUES (?,?,?,?,?,?,?)',
            (
                'owner',
                review['review_id'],
                review['fact_id'],
                'accept',
                review['source_commit_id'],
                review['source_item_revision'],
                review['source_content_hash'],
            ),
        )
    assert database.connection.execute('SELECT count(*) FROM cf_memory_review_apply_guard').fetchone()[0] == 0
