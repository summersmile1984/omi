"""Memory handoff, real Candidate creation and signed receipt retries on App SQL."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pytest

from candidate_kernel_association import RecurrenceInboxStatus
from memory_kernel_recurrence import CanonicalRecurrenceSignal
from memory_kernel_consolidation import ConsolidationAgentBatch
from memory_consolidation_apply import apply_consolidation_batch
from recurrence_inbox import plan_handoff
from recurrence_routes import PROCESSOR_PATH
from internal_auth import create_request_context
from test_candidate_routes import api, call
from test_memory_mutation_lock import target
from test_memory_consolidation_apply import context, decision, environment


@pytest.fixture
def env(target):
    return environment(target[0])


def rows(env, table):
    return [dict(row) for row in env.APP_DB.connection.execute('SELECT * FROM ' + table)]


def signal(memory_id='source-1', **updates):
    current = datetime.now(timezone.utc)
    return CanonicalRecurrenceSignal.model_validate(
        dict(
            signal_id='signal-1',
            title='Prepare the weekly report',
            objective='Keep the report current',
            anchor_task_description='Collect the current metrics',
            occurrence_count=3,
            distinct_day_count=3,
            unresolved=True,
            confidence=0.9,
            first_seen_at=current - timedelta(days=2),
            last_seen_at=current,
            evidence_refs=[{'kind': 'memory_item', 'scope': 'canonical', 'id': memory_id}],
        )
        | updates
    )


def enqueue(env, *signals):
    tx, pending = asyncio.run(plan_handoff(env, 'owner', 0, signals, timestamp=datetime.now(timezone.utc)))
    asyncio.run(tx.commit())
    return pending[0] if pending else rows(env, 'cf_task_recurrence_inbox')[0]['receipt_id']


def deliver(api, env, identity, *, uid='owner', generation=0, authority='internal'):
    encoded, signature = create_request_context(
        uid,
        env.INTERNAL_ASSERTION_SECRET,
        audience='api-core',
        method='POST',
        path=PROCESSOR_PATH,
        request_id='recurrence-test',
        authority=authority,
    )
    return call(
        api,
        'POST',
        PROCESSOR_PATH,
        request_headers={'x-omi-auth-context': encoded, 'x-omi-internal-signature': signature},
        json={'receipt_id': identity, 'account_generation': generation},
    )


def apply(env, snapshot, *signals):
    return asyncio.run(
        apply_consolidation_batch(
            env,
            snapshot,
            ConsolidationAgentBatch(
                decisions=[decision(item) for item in snapshot.pending_items], recurrence_signals=list(signals)
            ),
            run_id='recurrence-test',
            now=datetime.now(timezone.utc),
        )
    )


def test_memory_commit_hands_off_before_signed_consumer_creates_original_candidate(api, env, target):
    database, _, create = target
    memory_id = create(content='The weekly report remains unfinished')
    snapshot = context(database, memory_id)
    original = signal(memory_id)
    output = apply(env, snapshot, original)
    assert output[memory_id].promotion['route'] == 'promote'
    receipt = rows(env, 'cf_task_recurrence_inbox')[0]
    assert receipt['status'] == 'pending' and rows(env, 'cf_candidates') == []
    assert any(m['kind'] == 'task_recurrence' and m['jobId'] == receipt['receipt_id'] for m in database.published)
    response = deliver(api, env, receipt['receipt_id'])
    assert response.status_code == 200 and response.json() == {'status': 'completed'}, response.text
    candidates = call(api, 'GET', '/v1/candidates').json()['candidates']
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate['capture_confidence'] == 0.9 and candidate['ownership_confidence'] == 0.5
    assert candidate['workstream_proposal']['title'] == original.title
    assert candidate['source_surface'] == 'memory_recurrence' and candidate['evidence_refs'] == [
        ref.model_dump(mode='json', exclude_none=True) for ref in original.evidence_refs
    ]
    first = rows(env, 'cf_task_recurrence_inbox')[0]['record_json']
    assert deliver(api, env, receipt['receipt_id']).status_code == 200
    assert rows(env, 'cf_task_recurrence_inbox')[0]['record_json'] == first
    accepted = call(api, 'POST', '/v1/candidates/' + candidate['candidate_id'] + '/accept')
    assert accepted.status_code == 200, accepted.text
    stream = call(api, 'GET', '/v1/workstreams/' + accepted.json()['workstream_id']).json()
    assert stream['workstream']['objective'] == original.objective
    exported = call(api, 'GET', '/v1/users/export').json()['task_data']['task_recurrence_inbox']
    assert len(exported) == 1 and exported[0]['status'] == 'completed'
    assert call(api, 'GET', '/v1/users/export', uid='other').json()['task_data']['task_recurrence_inbox'] == []
    assert not rows(env, 'cf_account_cutover')


@pytest.mark.parametrize(
    'update',
    [
        {'unresolved': False},
        {'occurrence_count': 1, 'distinct_day_count': 1},
        {'distinct_day_count': 1},
        {'confidence': 0.69},
    ],
)
def test_original_threshold_completes_receipt_without_creating_a_candidate(api, env, update):
    identity = enqueue(env, signal(**update))
    response = deliver(api, env, identity)
    assert response.status_code == 200, response.text
    receipt = json.loads(rows(env, 'cf_task_recurrence_inbox')[0]['record_json'])
    assert receipt['last_outcome'] == 'below_threshold' and receipt['status'] == 'completed'
    assert rows(env, 'cf_candidates') == []


def test_first_signal_is_frozen_across_duplicate_batch_rewording_and_completed_replay(api, env):
    first = signal()
    second = first.model_copy(update={'signal_id': 'signal-2', 'title': 'Changed model wording'})
    identity = enqueue(env, first, second)
    raw = rows(env, 'cf_task_recurrence_inbox')[0]['record_json']
    assert json.loads(raw)['signal']['title'] == first.title
    assert enqueue(env, second) == identity
    assert rows(env, 'cf_task_recurrence_inbox')[0]['record_json'] == raw
    assert deliver(api, env, identity).status_code == 200
    completed = rows(env, 'cf_task_recurrence_inbox')[0]['record_json']
    enqueue(env, second)
    assert rows(env, 'cf_task_recurrence_inbox')[0]['record_json'] == completed
    assert len(rows(env, 'cf_candidates')) == 1


def test_candidate_commit_followed_by_receipt_failure_recovers_without_duplicate_or_changed_proposal(api, env):
    identity = enqueue(env, signal())
    env.APP_DB.connection.execute(
        "CREATE TRIGGER fail_recurrence_ack BEFORE UPDATE ON cf_task_recurrence_inbox WHEN json_extract(NEW.record_json,'$.status')='completed' BEGIN SELECT RAISE(ABORT,'controlled receipt outage'); END"
    )
    response = deliver(api, env, identity)
    assert response.status_code == 503, response.text
    before = rows(env, 'cf_candidates')
    assert len(before) == 1 and rows(env, 'cf_task_recurrence_inbox')[0]['status'] == 'pending'
    env.APP_DB.connection.execute('DROP TRIGGER fail_recurrence_ack')
    assert deliver(api, env, identity).status_code == 200
    assert rows(env, 'cf_candidates') == before
    receipt = json.loads(rows(env, 'cf_task_recurrence_inbox')[0]['record_json'])
    assert receipt['status'] == 'completed' and receipt['attempts'] == 2 and receipt['last_error_code'] is None


def test_failed_handoff_rolls_back_memory_result_and_sends_no_recurrence_hint(env, target):
    database, _, create = target
    memory_id = create()
    snapshot = context(database, memory_id)
    before = dict(database.row(memory_id))
    env.APP_DB.connection.execute(
        "CREATE TRIGGER reject_recurrence AFTER INSERT ON cf_task_recurrence_inbox BEGIN SELECT RAISE(ABORT,'handoff unavailable'); END"
    )
    with pytest.raises(Exception, match='handoff unavailable'):
        apply(env, snapshot, signal(memory_id))
    assert dict(database.row(memory_id)) == before and rows(env, 'cf_task_recurrence_inbox') == []
    assert rows(env, 'cf_memory_apply_guard') == rows(env, 'cf_candidate_write_guard') == []
    assert not any(m['kind'] == 'task_recurrence' for m in database.published)


def test_queue_hint_failure_preserves_committed_handoff_for_later_delivery(api, env, target):
    database, _, create = target
    memory_id = create()
    snapshot = context(database, memory_id)

    async def reject(_):
        raise RuntimeError('controlled queue outage')

    env.JOBS.send = reject
    output = apply(env, snapshot, signal(memory_id))
    assert output[memory_id].promotion['route'] == 'promote'
    identity = rows(env, 'cf_task_recurrence_inbox')[0]['receipt_id']
    assert deliver(api, env, identity).status_code == 200
    assert len(rows(env, 'cf_candidates')) == 1


def test_signed_internal_authority_owner_and_generation_are_required(api, env):
    identity = enqueue(env, signal())
    assert call(api, 'POST', PROCESSOR_PATH, json={'receipt_id': identity, 'account_generation': 0}).status_code == 401
    assert deliver(api, env, identity, uid='other').status_code == 404
    assert deliver(api, env, identity, generation=1).status_code == 409
    env.APP_DB.before_write = lambda: env.APP_DB.connection.execute(
        "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',1,1)"
    )
    assert deliver(api, env, identity).status_code == 409
    assert rows(env, 'cf_candidates') == [] and rows(env, 'cf_task_recurrence_inbox')[0]['status'] == 'pending'


def test_actual_consolidation_dispatch_persists_signal_before_cycle_watermark(api, env, target):
    from test_memory_consolidation_context import services
    from memory_consolidation_dispatch import process_consolidation_dispatch
    from memory_apply_intake import load_memory_control

    database, _, create = target
    memory_id = create(content='The weekly report remains unfinished')
    snapshot = context(database, memory_id)
    model = services(
        database,
        output={
            'decisions': [decision(snapshot.pending_items[0]).model_dump(mode='json')],
            'recurrence_signals': [signal(memory_id).model_dump(mode='json')],
        },
    )
    env.AI, env.MEMORY_VECTORS = model.AI, model.MEMORY_VECTORS
    for _ in range(3):
        result = asyncio.run(process_consolidation_dispatch(env, 'owner', 0))
        if not result['pending']:
            break
    assert not result['pending']
    _, control = asyncio.run(load_memory_control(env, 'owner'))
    assert control.last_consolidation_run_at is not None
    receipt = rows(env, 'cf_task_recurrence_inbox')[0]
    assert receipt['status'] == 'pending'
    assert len([call for call in model.AI.calls if 'messages' in call[1]]) == 1
    assert deliver(api, env, receipt['receipt_id']).status_code == 200
    assert len(rows(env, 'cf_candidates')) == 1


def test_concurrent_first_receipt_defers_memory_apply_and_retains_first_signal(env, target):
    from candidate_kernel_association import RecurrenceInboxReceipt
    from recurrence_kernel import _receipt_id
    from memory_kernel_consolidation import ConsolidationApplySkipped
    from memory_consolidation_runner import _deferred

    database, _, create = target
    memory_id = create()
    snapshot = context(database, memory_id)
    first = signal(memory_id)
    changed = first.model_copy(update={'title': 'A later model wording'})
    identity = _receipt_id('owner', first.stable_loop_key, 0)
    receipt = RecurrenceInboxReceipt(
        receipt_id=identity,
        loop_key=first.stable_loop_key,
        account_generation=0,
        status=RecurrenceInboxStatus.pending,
        signal=first,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    def competitor():
        database.connection.execute(
            "INSERT INTO cf_candidate_write_guard(uid,account_generation,recurrences_json) VALUES ('owner',0,?)",
            (json.dumps([{'id': identity, 'before': None}]),),
        )
        database.connection.execute(
            "INSERT INTO cf_task_recurrence_inbox(uid,receipt_id,record_json) VALUES ('owner',?,?)",
            (identity, receipt.model_dump_json()),
        )
        database.connection.execute("DELETE FROM cf_candidate_write_guard WHERE uid='owner'")

    database.before_write = competitor
    before = dict(database.row(memory_id))
    with pytest.raises(ConsolidationApplySkipped, match='recurrence_changed') as rejected:
        apply(env, snapshot, changed)
    assert _deferred(rejected.value)
    assert dict(database.row(memory_id)) == before
    apply(env, snapshot, changed)
    assert json.loads(rows(env, 'cf_task_recurrence_inbox')[0]['record_json'])['signal']['title'] == first.title
