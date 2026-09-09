"""Real native correction handlers, canonical rules and full D1 SQL migrations.

Expected pending Short-term / graph retraction behavior is defined by upstream
update_canonical_memory_content and INV-MEM-4, not by the old flat UPDATE path.
"""

import json
import ast
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from typing import Any, Dict, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from memory_apply_edit import content_edit_patch
from memory_apply_item import read_item
from memory_kernel_admission import REQUIRED_PROCESSOR_ID, REQUIRED_PROCESSOR_VERSION
from memory_kernel_item import MemoryItem, MemoryLayer
from memory_kernel_operations import MemoryOperation
from memory_kernel_short_term_lifecycle import default_short_term_expiry
from test_memory_apply_intake import state
from test_memory_mutation_lock import target as target


def test_edit_patch_matches_the_upstream_content_correction_policy(target):
    database, _, create = target
    memory_id = create()
    item = read_item(database.row(memory_id))
    item = item.model_copy(
        update={
            'promotion': {
                'route': 'promote',
                'graph_plan': {'old': True},
                'admission_receipt': {'old': True},
                'processing_receipt': {'prior': True},
                'submission': {'prior': True},
                'processing_history': [{'n': n} for n in range(12)],
                'unrelated_metadata': 'keep',
            }
        }
    )
    now = datetime.now(timezone.utc)
    captured = {}

    def apply(uid, memory_id, **options):
        logical, patch = options['build_patch'](item, now)
        captured.update(logical, **patch)
        return item, item

    # Execute the original upstream function with only its Firestore call
    # controlled. This compares business policy, not source spellings.
    source = Path(__file__).parents[5] / 'backend/utils/memory/canonical_memory_adapter.py'
    tree = ast.parse(source.read_text())
    names = {'update_canonical_memory_content', '_clear_settled_promotion_route'}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    nodes += [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == '_SETTLED_PROMOTION_FIELDS' for target in node.targets)
    ]
    scope = {
        'Any': Any,
        'Tuple': Tuple,
        'Payload': Dict[str, Any],
        'MemoryItem': MemoryItem,
        'MemoryLayer': MemoryLayer,
        'datetime': datetime,
        'hashlib': __import__('hashlib'),
        'default_short_term_expiry': default_short_term_expiry,
        'REQUIRED_PROCESSOR_ID': REQUIRED_PROCESSOR_ID,
        'REQUIRED_PROCESSOR_VERSION': REQUIRED_PROCESSOR_VERSION,
        'REQUIRED_PROMOTION_STATUS_PENDING': 'pending',
        'REQUIRED_PROCESSING_STATUS_PENDING': 'pending_processing',
        'belief_model_enabled': lambda: False,
        '_apply_canonical_user_mutation': apply,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), scope)
    scope['update_canonical_memory_content']('owner', memory_id, 'Correction', db_client=object())
    actual = content_edit_patch(item, 'Correction', now)
    assert actual.pop('updated_at') == now.isoformat()
    captured['expires_at'] = captured['expires_at'].isoformat()
    assert actual == captured


def test_content_correction_commits_pending_item_receipt_head_and_delete_outboxes(target):
    database, request, create = target
    memory_id = create(content='Original claim', predicate='resides_in', arguments={'city': 'London'})
    original = read_item(database.row(memory_id))
    response = request('PATCH', f'/v3/memories/{memory_id}', body={'value': '  Corrected claim 中文 🌊  '})
    assert response.status_code == 200, response.text
    row = database.row(memory_id)
    item = read_item(row)
    assert item.content == 'Corrected claim 中文 🌊'
    assert item.tier.value == 'short_term' and item.processing_state.value == 'pending'
    assert item.user_asserted and item.item_revision == original.item_revision + 1
    assert item.version == original.version + 1 and item.source_commit_id == original.source_commit_id
    assert item.predicate is None and item.arguments == {} and not item.graph_ready and not item.kg_extracted
    assert row['edited'] == row['reviewed'] == row['user_review'] == 1
    assert item.promotion['submission']['submission_id'] == f'{memory_id}:revision:{item.item_revision}'
    assert item.promotion['processing_status'] == 'pending_processing'
    snapshot = state(database)
    assert len(snapshot['cf_memory_commits']) == len(snapshot['cf_memory_operations']) == 2
    assert len(snapshot['cf_usage_sources']) == 1
    assert snapshot['cf_memory_apply_guard'] == []
    control = snapshot['cf_memory_apply_control'][0]
    assert control['head_commit_id'] == item.ledger_commit_id and control['commit_sequence'] == item.ledger_sequence
    receipt = next(
        MemoryOperation.model_validate_json(r['operation_json'])
        for r in snapshot['cf_memory_operations']
        if json.loads(r['operation_json'])['operation_type'] == 'user_mutation'
    )
    assert receipt.logical_payload.memory_text == item.content
    assert receipt.committed_memory_item_ids == [memory_id]
    events = [
        json.loads(r['event_json']) for r in snapshot['cf_memory_outbox'] if r['commit_id'] == item.ledger_commit_id
    ]
    assert len(events) == 2 and all(event['payload']['action'] == 'delete' for event in events)
    assert all(event['payload']['item_revision'] == item.item_revision for event in events)
    projection = snapshot['cf_vector_projection_outbox'][0]
    assert projection['desired_version'] == item.item_revision and projection['operation'] == 'delete'


@pytest.mark.parametrize('tier', ['short_term', 'long_term', 'archive'])
def test_pre_journal_memory_is_adopted_in_place_by_the_actual_owner_correction(target, tier):
    database, request, _ = target
    now = int(time.time())
    database.connection.execute(
        'INSERT INTO cf_memories(uid,id,content,memory_tier,valid_at,created_at,updated_at) ' 'VALUES (?,?,?,?,?,?,?)',
        ('owner', 'historical', 'Historical claim', tier, now, now, now),
    )
    original = database.row('historical')
    assert original['canonical_metadata_json'] == '{}'
    assert request('PATCH', '/v3/memories/historical', body={'value': 'Foreign'}, uid='other').status_code == 404
    response = request('PATCH', '/v3/memories/historical', body={'value': 'Corrected historical claim'})
    assert response.status_code == 200, response.text
    updated = read_item(database.row('historical'))
    assert updated.item_revision == original['item_revision'] + 1
    assert updated.tier.value == 'short_term' and updated.processing_state.value == 'pending'
    assert updated.promotion['historical_materialization'] is True
    assert updated.source_commit_id is None  # No invented historical journal commit.
    snapshot = state(database)
    assert len(snapshot['cf_memories']) == len(snapshot['cf_memory_commits']) == 1
    assert updated.ledger_commit_id == snapshot['cf_memory_commits'][0]['commit_id']
    assert not updated.ledger_commit_id.startswith('historical_')
    assert snapshot['cf_usage_sources'] == []


@pytest.mark.parametrize('change', ['revision', 'source', 'head', 'generation', 'metadata', 'delete'])
def test_concurrent_target_or_account_change_cannot_commit_stale_correction(target, change):
    database, request, create = target
    memory_id = create()
    expected = {}

    def race():
        if change == 'head':
            control = json.loads(
                database.connection.execute('SELECT control_json FROM cf_memory_apply_control').fetchone()[0]
            )
            control['head_commit_id'] = 'concurrent-head'
            database.connection.execute(
                'UPDATE cf_memory_apply_control SET head_commit_id=?,control_json=?',
                ('concurrent-head', json.dumps(control)),
            )
        elif change == 'generation':
            database.connection.execute(
                "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',1,1)"
            )
        elif change == 'metadata':
            database.connection.execute(
                "UPDATE cf_memories SET canonical_metadata_json=json_set(canonical_metadata_json,'$.slot','changed') WHERE id=?",
                (memory_id,),
            )
        else:
            assignment = {
                'revision': "content='Concurrent correction'",
                'source': "source_state='tombstoned'",
                'delete': 'deleted_at=1',
            }[change]
            database.connection.execute('UPDATE cf_memories SET ' + assignment + ' WHERE id=?', (memory_id,))
        expected.update(state(database))

    database.before_write = race
    response = request('PATCH', f'/v3/memories/{memory_id}', body={'value': 'Stale correction'})
    assert response.status_code == 503, response.text
    assert state(database) == expected


@pytest.mark.parametrize('table', ['cf_memory_operations', 'cf_memory_commits', 'cf_memory_outbox'])
def test_late_failure_rolls_back_correction_and_every_journal_and_projection_write(target, table):
    database, request, create = target
    memory_id = create()
    before = state(database)
    database.connection.execute(
        f"CREATE TRIGGER fail_edit BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'fault'); END"
    )
    response = request('PATCH', f'/v3/memories/{memory_id}', body={'value': 'Not committed'})
    assert response.status_code == 503, response.text
    assert state(database) == before


def test_corrupt_canonical_metadata_cannot_be_reinterpreted_as_historical(target):
    database, request, create = target
    memory_id = create()
    database.connection.execute(
        "UPDATE cf_memories SET canonical_metadata_json='{" + '"content":"shadow"' + "}' WHERE id=?", (memory_id,)
    )
    before = state(database)
    assert request('PATCH', f'/v3/memories/{memory_id}', body={'value': 'Unsafe'}).status_code == 503
    assert state(database) == before


@pytest.mark.parametrize(
    'assignment', ["status='hidden'", "status='superseded'", "superseded_by='successor'", 'invalid_at=1']
)
def test_closed_rows_cannot_be_resurrected_by_content_edit(target, assignment):
    database, request, create = target
    memory_id = create()
    database.connection.execute('UPDATE cf_memories SET ' + assignment + ' WHERE id=?', (memory_id,))
    before = state(database)
    assert request('PATCH', f'/v3/memories/{memory_id}', body={'value': 'Must stay closed'}).status_code == 404
    assert state(database) == before
