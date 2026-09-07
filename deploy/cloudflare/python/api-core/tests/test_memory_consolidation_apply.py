"""Upstream L2 decisions through real D1 SQL, then the public memory/review API.

Only the model response is controlled. This does not claim that maintenance
scheduling, candidate retrieval or a hosted model has been exercised.
"""

import ast
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from memory_apply_item import read_item
from memory_apply_intake import load_memory_control
from memory_consolidation_apply import apply_consolidation_batch
from memory_consolidation_policy import consolidation_route_patch, required_processing_patch
import memory_kernel_consolidation as policy
from memory_kernel_apply import ApplyStatus, apply_long_term_patch_transaction, build_patch_mutation_identity
from memory_kernel_operations import MemoryOperation
from memory_kernel_required_promotion import REQUIRED_PROCESSING_STATUS_PROCESSED, REQUIRED_PROMOTION_STATUS_PENDING
from test_memory_mutation_lock import target
from test_memory_review_routes import journal


def environment(database):
    async def send(message):
        database.published.append(message)

    return SimpleNamespace(APP_DB=database, JOBS=SimpleNamespace(send=send))


def context(database, *memory_ids, candidates=()):
    result = policy.ConsolidationContext(
        uid='owner', pending_items=[read_item(database.row(key)) for key in memory_ids]
    )
    for item in result.pending_items:
        result.candidates_by_anchor[item.memory_id] = [
            policy.ConsolidationCandidate(
                anchor_memory_id=item.memory_id,
                memory_id=other.memory_id,
                content=other.content,
                score=0.8,
                tier=other.tier.value,
                captured_at=other.captured_at.isoformat(),
                sensitivity_labels=tuple(other.sensitivity_labels),
                user_rejected=(other.promotion or {}).get('user_review') is False,
            )
            for other in [read_item(database.row(key)) for key in candidates]
        ]
    return result


def decision(item, route='promote', **updates):
    return policy.ConsolidationAgentDecision.model_validate(
        {
            'source_memory_id': item.memory_id,
            'route': route,
            'memory_text': 'Prefers jasmine tea',
            'evidence_ids': [record.evidence_id for record in item.evidence],
            'subject_entity_id': 'user',
            'predicate': 'prefers_tea',
            'arguments': {'tea': 'jasmine'},
            'relationship_to_user': 'self',
            'aboutness': 'primary_user',
            'basis_for_memory': 'explicit',
            'rationale': 'Explicit preference',
            **updates,
        }
    )


def execute(database, snapshot, *decisions):
    return asyncio.run(
        apply_consolidation_batch(
            environment(database),
            snapshot,
            policy.ConsolidationAgentBatch(decisions=list(decisions)),
            run_id='consolidation-test',
            now=datetime.now(timezone.utc),
        )
    )


@pytest.mark.parametrize('route', ['promote', 'archive', 'review', 'reject'])
@pytest.mark.parametrize('required', [False, True])
def test_l2_route_commits_normalization_receipt_graph_or_review_and_is_readable(target, route, required):
    database, request, create = target
    memory_id = create(content='I prefer jasmine tea')
    if required:
        response = request('PATCH', '/v3/memories/' + memory_id, body={'value': 'I now prefer jasmine tea'})
        assert response.status_code == 200, response.text
    snapshot = context(database, memory_id)
    original = snapshot.pending_items[0]
    assert policy.is_pending_required_processing(original) == required
    initial_commits = database.connection.execute('SELECT count(*) FROM cf_memory_commits').fetchone()[0]
    result = execute(database, snapshot, decision(original, route))
    item = read_item(database.row(memory_id))
    assert item == result[memory_id]
    assert item.item_revision == original.item_revision + 1 + int(required)
    assert item.processing_state.value == 'processed'
    if required:
        assert item.promotion['processing_receipt']['input_item_revision'] == original.item_revision
        assert item.promotion['processing_receipt']['output_item_revision'] == original.item_revision + 1
    assert item.promotion['route'] == route
    assert item.tier.value == ('long_term' if route == 'promote' else 'archive')
    assert item.status.value == ('hidden' if route == 'reject' else 'active')
    assert database.connection.execute('SELECT count(*) FROM cf_memory_commits').fetchone()[
        0
    ] == initial_commits + 1 + int(required)
    assert database.connection.execute('SELECT count(*) FROM cf_memory_operations').fetchone()[
        0
    ] == initial_commits + 1 + int(required)
    assert database.connection.execute('SELECT count(*) FROM cf_memory_apply_guard').fetchone()[0] == 0
    graphs = database.connection.execute('SELECT * FROM cf_memory_graph_assertions').fetchall()
    assert len(graphs) == int(route == 'promote')
    if graphs:
        assertion = json.loads(graphs[0]['assertion_json'])
        assert assertion['item_revision'] == item.item_revision
        assert assertion['commit_id'] == item.ledger_commit_id and item.graph_ready
    queues = request('GET', '/v3/memories/review-queue').json()
    assert len(queues) == int(route == 'review')
    if queues:
        review = queues[0]
        assert review['source_commit_id'] == item.ledger_commit_id
        assert review['source_item_revision'] == item.item_revision
        assert review['source_content_hash'] == item.content_hash
        accepted = request(
            'POST', '/v3/memories/review-queue/' + review['review_id'] + '/resolve', body={'decision': 'accept'}
        )
        assert accepted.status_code == 200, accepted.text
        assert read_item(database.row(memory_id)).processing_state.value == 'pending'
    before = journal(database)
    with pytest.raises(policy.ConsolidationApplySkipped, match='source_changed'):
        execute(database, snapshot, decision(original, route))
    assert journal(database) == before


def test_replace_promotes_new_source_and_supersedes_old_graph_in_one_apply(target):
    database, _, create = target
    old_id = create(content='Prefers black tea')
    old_context = context(database, old_id)
    execute(database, old_context, decision(old_context.pending_items[0]))
    new_id = create(content='Now prefers jasmine tea')
    snapshot = context(database, new_id, candidates=[old_id])
    outputs = execute(
        database,
        snapshot,
        decision(snapshot.pending_items[0], reconciliation='replace', target_memory_id=old_id, supersedes=[old_id]),
    )
    old, new = outputs[old_id], outputs[new_id]
    assert old.status.value == 'superseded' and old.superseded_by == new_id
    assert old.ledger_commit_id == new.ledger_commit_id
    graphs = database.connection.execute('SELECT memory_id FROM cf_memory_graph_assertions').fetchall()
    assert [row[0] for row in graphs] == [new_id]


@pytest.mark.parametrize('failure', ['missing', 'duplicate', 'unknown_evidence', 'foreign_reference', 'unsafe_subject'])
def test_invalid_model_partition_or_provenance_cannot_write_any_member(target, failure):
    database, _, create = target
    snapshot = context(database, create(), create())
    outputs = [decision(item) for item in snapshot.pending_items]
    if failure == 'missing':
        outputs.pop()
    elif failure == 'duplicate':
        outputs[1] = outputs[0]
    else:
        changes = {
            'unknown_evidence': {'evidence_ids': ['foreign-evidence']},
            'foreign_reference': {
                'target_memory_id': 'foreign',
                'reconciliation': 'replace',
                'supersedes': ['foreign'],
            },
            'unsafe_subject': {'relationship_to_user': 'encountered'},
        }[failure]
        outputs[1] = decision(snapshot.pending_items[1], **changes)
    before = journal(database)
    with pytest.raises(policy.ConsolidationApplySkipped, match='output_invalid'):
        execute(database, snapshot, *outputs)
    assert journal(database) == before


@pytest.mark.parametrize('target_table', ['cf_memory_graph_assertions', 'cf_memory_review_queue', 'cf_memory_commits'])
def test_late_storage_failure_rolls_back_every_normalization_route_graph_and_review(target, target_table):
    database, request, create = target
    identifiers = [create(), create()]
    for memory_id in identifiers:
        assert request('PATCH', '/v3/memories/' + memory_id, body={'value': 'Pending normalization'}).status_code == 200
    snapshot = context(database, *identifiers)
    assert all(policy.is_pending_required_processing(item) for item in snapshot.pending_items)
    before = journal(database)
    database.connection.execute(
        f'CREATE TRIGGER test_late_failure BEFORE INSERT ON {target_table} '
        "BEGIN SELECT RAISE(ABORT, 'controlled late failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match='controlled late failure'):
        execute(database, snapshot, decision(snapshot.pending_items[0]), decision(snapshot.pending_items[1], 'review'))
    assert journal(database) == before
    assert database.connection.execute('SELECT count(*) FROM cf_memory_graph_assertions').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['content', 'lock', 'generation', 'source', 'head'])
def test_concurrent_memory_or_account_change_rejects_stale_whole_batch(target, change):
    database, _, create = target
    snapshot = context(database, create(), create())
    expected = {}

    def race():
        if change == 'generation':
            database.connection.execute(
                "INSERT INTO cf_account_cutover(uid, account_generation, updated_at) VALUES('owner',1,1)"
            )
        elif change == 'head':
            database.connection.execute(
                "UPDATE cf_memory_apply_control SET control_json=json_set(control_json,'$.head_commit_id','new-head'),head_commit_id='new-head'"
            )
        else:
            assignment = {'content': "content='Changed'", 'lock': 'is_locked=1', 'source': "source_state='tombstoned'"}[
                change
            ]
            database.connection.execute(
                'UPDATE cf_memories SET ' + assignment + ' WHERE id=?', (snapshot.pending_items[1].memory_id,)
            )
        expected.update(journal(database))

    database.before_write = race
    with pytest.raises(sqlite3.IntegrityError):
        execute(database, snapshot, *[decision(item) for item in snapshot.pending_items])
    assert journal(database) == expected


def test_patch_builders_equal_original_upstream_apply_requests(target):
    database, _, create = target
    item = read_item(database.row(create()))
    now = datetime.now(timezone.utc)
    _, control = asyncio.run(load_memory_control(environment(database), 'owner'))
    outcome = decision(item)
    processed = policy._processed_from_consolidation_decision(item, outcome)
    captured = {}

    def persist(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status=ApplyStatus.committed)

    scope = {
        **vars(policy),
        'ApplyStatus': ApplyStatus,
        'build_patch_mutation_identity': build_patch_mutation_identity,
        '_read_control_state': lambda *args, **kwargs: control,
        'apply_long_term_patch_firestore': persist,
        'REQUIRED_PROMOTION_STATUS_PENDING': REQUIRED_PROMOTION_STATUS_PENDING,
        'REQUIRED_PROCESSING_STATUS_PROCESSED': REQUIRED_PROCESSING_STATUS_PROCESSED,
    }

    def original(relative, name):
        path = Path(__file__).parents[5] / relative
        node = next(
            node for node in ast.parse(path.read_text()).body if isinstance(node, ast.FunctionDef) and node.name == name
        )
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
        return scope[name]

    original('backend/utils/memory/canonical_required_processing.py', '_apply_processed_result')(
        item, processed, attempt_count=1, db_client=object(), now=now
    )
    operation, patch = required_processing_patch(item, processed, control, now)
    assert patch == captured['patch_payload']
    excluded = {'created_at', 'updated_at'}
    assert operation.model_dump(exclude=excluded) == captured['proposed_operation'].model_dump(exclude=excluded)
    result = apply_long_term_patch_transaction(
        control_state=control,
        operation=operation,
        patch_payload={**patch, 'existing_item': item, 'evidence': item.evidence},
    )
    source = result.memory_items[0]
    control = result.control_state
    original('backend/utils/memory/canonical_consolidation.py', 'apply_consolidation_decision')(
        'owner',
        decision=outcome,
        pending_by_id={item.memory_id: source},
        control=control,
        run_id='test',
        now=now,
        db_client=object(),
    )
    operation, patch = consolidation_route_patch(source, outcome, control, 'test', now)
    assert patch == captured['patch_payload']
    # Operation timestamps are observation metadata; identity and logical payload must match exactly.
    left, right = operation.model_dump(), captured['proposed_operation'].model_dump()
    for values in (left, right):
        values.pop('created_at', None)
        values.pop('updated_at', None)
    assert left == right
