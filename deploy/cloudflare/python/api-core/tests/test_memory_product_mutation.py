"""Native metadata policies, canonical journal and graph atomicity."""

import ast
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from memory_apply_intake import _insert_rows, _stored_item, control_statement
from memory_apply_item import read_item
from memory_kernel_apply import ApplyStatus, apply_long_term_patch_transaction
from memory_kernel_item import MemoryItem, MemoryLayer, ProcessingState
from memory_kernel_promotion import MemoryGraphAssertion
from memory_privacy_receipts import privacy_receipt_id
from memory_product_mutation import product_patch, review_patch, visibility_patch
from memory_routes import MemoryCreate, _batch_row
from test_memory_apply_intake import state
from test_memory_kernel import control, promotion_case
from test_memory_mutation_lock import target

CASES = [
    ('PATCH', '/visibility?value=public', None, 'visibility', 'public'),
    ('POST', '/review?value=false', None, 'user_review', 0),
    ('PATCH', '/read', {'is_read': True, 'is_dismissed': True}, 'is_read', 1),
    ('PATCH', '/baseline?value=true', None, 'is_baseline', 1),
]


def snapshot(database):
    return {
        **state(database),
        **{
            table: [dict(row) for row in database.connection.execute('SELECT * FROM ' + table)]
            for table in ('cf_memory_graph_assertions', 'cf_feedback_events')
        },
    }


@pytest.mark.parametrize('method,suffix,body,field,value', CASES)
def test_product_mutation_commits_head_receipt_and_preserves_other_fields(target, method, suffix, body, field, value):
    db, request, create = target
    memory_id = create(content='Original memory')
    db.connection.execute(
        "UPDATE cf_memories SET category='manual',tags_json='[\"retained\"]' WHERE id=?", (memory_id,)
    )
    before = read_item(db.row(memory_id))
    assert request(method, '/v3/memories/' + memory_id + suffix, body=body, uid='other').status_code == 404
    result = request(method, '/v3/memories/' + memory_id + suffix, body=body)
    assert result.status_code == 200, result.text
    row = db.row(memory_id)
    updated = read_item(row)
    assert row[field] == value
    assert updated.item_revision == before.item_revision + 1 and updated.version == before.version + 1
    assert (
        updated.content == before.content
        and updated.tier == before.tier
        and updated.content_hash == before.content_hash
    )
    assert updated.promotion['category'] == 'manual' and updated.promotion['tags'] == ['retained']
    if field != 'visibility':
        assert updated.promotion[field] == bool(value)
    after = snapshot(db)
    assert len(after['cf_memory_operations']) == len(after['cf_memory_commits']) == 2
    assert updated.ledger_commit_id == after['cf_memory_apply_control'][0]['head_commit_id']
    assert len(after['cf_usage_sources']) == 1 and after['cf_memory_apply_guard'] == []
    receipts = [json.loads(row['operation_json']) for row in after['cf_memory_operations']]
    assert sum(row['operation_type'] == 'user_mutation' for row in receipts) == 1
    assert len(after['cf_feedback_events']) == (1 if field == 'user_review' else 0)


@pytest.mark.parametrize(
    'kind,value',
    [
        ('visibility', 'public'),
        ('review', False),
        ('review', True),
        ('metadata', {'is_read': True, 'is_baseline': False}),
    ],
)
def test_patch_matches_upstream_with_default_belief_mode(target, kind, value):
    db, _, create = target
    item = read_item(db.row(create())).model_copy(
        update={
            'promotion': {'required': True, 'processing_status': 'processing_rejected', 'retained': 'metadata'},
        }
    )
    now = datetime.now(timezone.utc)
    captured = {}

    def apply(uid, memory_id, **options):
        logical, patch = options['build_patch'](item, now)
        captured.update(logical=logical, patch=patch)
        return item, item

    functions = {
        'visibility': 'update_canonical_memory_visibility',
        'review': 'update_canonical_memory_review',
        'metadata': 'update_canonical_memory_product_fields',
    }
    source = Path(__file__).parents[5] / 'backend/utils/memory/canonical_memory_adapter.py'
    nodes = [
        node
        for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == functions[kind]
    ]
    scope = {
        'Any': Any,
        'Dict': Dict,
        'List': List,
        'Optional': Optional,
        'Tuple': Tuple,
        'Payload': Dict[str, Any],
        'MemoryItem': MemoryItem,
        'MemoryLayer': MemoryLayer,
        'ProcessingState': ProcessingState,
        'datetime': datetime,
        '_ALLOWED_MEMORY_VISIBILITIES': {'private', 'public', 'shared'},
        '_apply_canonical_user_mutation': apply,
        'belief_model_enabled': lambda: False,
        'invalidate_kg_for_memory_retraction': lambda *args, **kwargs: None,
        'REQUIRED_PROCESSING_STATUS_PENDING': 'pending_processing',
        'REQUIRED_PROCESSING_STATUS_REJECTED': 'processing_rejected',
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), scope)
    function = scope[functions[kind]]
    if kind == 'metadata':
        function('owner', item.memory_id, **value, db_client=object())
        logical, patch, _ = product_patch(item, now, value)
    else:
        function('owner', item.memory_id, value, db_client=object())
        logical, patch, _ = (visibility_patch if kind == 'visibility' else review_patch)(item, now, value)
    assert (logical, patch) == (captured['logical'], captured['patch'])


def test_rejection_and_reacceptance_keep_pending_processing_policy(target):
    db, request, create = target
    memory_id = create()
    assert request('PATCH', '/v3/memories/' + memory_id, body={'value': 'Corrected claim'}).status_code == 200
    for value, processing in [('false', 'processing_rejected'), ('true', 'pending_processing')]:
        result = request('POST', f'/v3/memories/{memory_id}/review?value={value}')
        assert result.status_code == 200, result.text
        item = read_item(db.row(memory_id))
        assert item.promotion['processing_status'] == processing
        assert item.processing_state == ProcessingState.pending
    assert len(snapshot(db)['cf_feedback_events']) == 2


@pytest.mark.parametrize(
    'table', ['cf_memory_operations', 'cf_memory_commits', 'cf_memory_outbox', 'cf_feedback_events']
)
def test_late_failure_rolls_back_complete_review(target, table):
    db, request, create = target
    memory_id = create()
    db.connection.execute(
        f"CREATE TRIGGER fail_mutation BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected unavailable'); END"
    )
    before = snapshot(db)
    assert request('POST', f'/v3/memories/{memory_id}/review?value=false').status_code == 503
    assert snapshot(db) == before


def seed_prior_graph(database):
    # An already admitted snapshot from the original pure promotion engine,
    # not a new public promotion path or a fabricated graph assertion.
    op, patch = promotion_case()
    result = apply_long_term_patch_transaction(control_state=control(), operation=op, patch_payload=patch)
    assert result.status == ApplyStatus.committed
    item = result.memory_items[0].model_copy(update={'account_generation': 0})
    row = _batch_row('owner', item.memory_id, MemoryCreate(content=item.content), int(item.updated_at.timestamp()))
    row = _stored_item(row, item)
    row['privacy_receipt_id'] = privacy_receipt_id(
        SimpleNamespace(MEMORY_PRIVACY_SECRET='memory-privacy-tests-secret-32-bytes'), 'owner', item.memory_id
    )

    async def seed():
        await database.batch(
            [
                *_insert_rows(database, 'cf_memories', [row]),
                control_statement(database, 'owner', result.control_state.model_copy(update={'account_generation': 0})),
            ]
        )

    asyncio.run(seed())
    return item.memory_id


def test_long_term_metadata_refreshes_graph_and_privacy_erases_it(target):
    db, request, _ = target
    memory_id = seed_prior_graph(db)
    for suffix in ['/baseline?value=true', '/visibility?value=public']:
        response = request('PATCH', '/v3/memories/' + memory_id + suffix)
        assert response.status_code == 200, response.text
        item = read_item(db.row(memory_id))
        rows = snapshot(db)['cf_memory_graph_assertions']
        assert len(rows) == 1
        assertion = MemoryGraphAssertion.model_validate_json(rows[0]['assertion_json'])
        assert assertion.item_revision == item.item_revision
        assert assertion.assertion_id == item.graph_assertion_id and assertion.commit_id == item.ledger_commit_id
    assert request('DELETE', '/v3/memories/' + memory_id).status_code == 200
    assert snapshot(db)['cf_memory_graph_assertions'] == []


def test_graph_failure_preserves_previous_graph_item_and_control(target):
    db, request, _ = target
    memory_id = seed_prior_graph(db)
    assert request('PATCH', f'/v3/memories/{memory_id}/baseline?value=true').status_code == 200
    db.connection.execute(
        "CREATE TRIGGER fail_graph BEFORE INSERT ON cf_memory_graph_assertions BEGIN SELECT RAISE(ABORT,'injected graph unavailable'); END"
    )
    before = snapshot(db)
    assert request('PATCH', f'/v3/memories/{memory_id}/visibility?value=public').status_code == 503
    assert snapshot(db) == before


@pytest.mark.parametrize('empty_guard', [False, True])
def test_graph_publish_requires_a_guard_for_this_target(target, empty_guard):
    db, request, _ = target
    memory_id = seed_prior_graph(db)
    assert request('PATCH', f'/v3/memories/{memory_id}/baseline?value=true').status_code == 200
    row = snapshot(db)['cf_memory_graph_assertions'][0]
    db.connection.execute('DELETE FROM cf_memory_graph_assertions')
    if empty_guard:
        db.connection.execute(
            'INSERT INTO cf_memory_apply_guard(uid,expected_control_json,account_generation,new_ids_json,operation_ids_json,expected_items_json) '
            "SELECT uid,control_json,account_generation,'[]','[]','[]' FROM cf_memory_apply_control WHERE uid='owner'"
        )
    with pytest.raises(sqlite3.IntegrityError, match='memory_graph_apply_required'):
        db.connection.execute('INSERT INTO cf_memory_graph_assertions VALUES (?,?,?,?,?)', tuple(row.values()))
