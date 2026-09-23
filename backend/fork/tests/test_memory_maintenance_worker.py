from __future__ import annotations

import os
from datetime import datetime, timezone
from functools import partial
from unittest import mock

import pytest

from fork import memory_maintenance_worker as worker
from fork.profile import ProfileError
from models.memory_apply import MemoryControlState
from tests.unit.test_workstream_association import _recurrence_signal
from utils.memory import short_term_promotion as maintenance
from utils.memory.canonical_consolidation import ConsolidationReport
from utils.task_intelligence import workstream_association as recurrence


class _Snapshot:
    def __init__(self, uid):
        self.reference = mock.Mock(path=f'canonical_memory_maintenance_registry/{uid}')
        self.payload = {'uid': uid, 'schema_version': 1}

    def to_dict(self):
        return self.payload


class _Query:
    def __init__(self, snapshots, after=''):
        self.snapshots = snapshots
        self.after = after
        self.size = len(snapshots)

    def where(self, field, operator, value):
        assert (field, operator) == ('uid', '>')
        return _Query(self.snapshots, value)

    def order_by(self, field):
        assert field == 'uid'
        return self

    def limit(self, size):
        result = _Query(self.snapshots, self.after)
        result.size = size
        return result

    def stream(self):
        return [snapshot for snapshot in self.snapshots if snapshot.payload['uid'] > self.after][: self.size]


def test_registry_pager_is_bounded_and_wraps_without_a_second_database_cursor():
    db = mock.Mock()
    db.collection.return_value = _Query([_Snapshot('uid-a'), _Snapshot('uid-b'), _Snapshot('uid-c')])
    inventory = worker.RegistryPager()

    assert inventory(db, 2) == ('uid-a', 'uid-b')
    assert inventory(db, 2) == ('uid-c', 'uid-a')
    assert inventory(db, 2) == ('uid-b', 'uid-c')
    assert db.document.call_count == 0


def test_registry_pager_rejects_a_malformed_identity():
    db = mock.Mock()
    malformed = _Snapshot('uid-a')
    malformed.reference.path = 'canonical_memory_maintenance_registry/other'
    db.collection.return_value = _Query([malformed])

    with pytest.raises(worker.RegistryInventoryUnavailable, match='malformed'):
        worker.RegistryPager()(db, 1)


@pytest.mark.parametrize('blocked', [False, True])
def test_processing_failure_cannot_be_hidden_by_successful_projection(monkeypatch, blocked):
    monkeypatch.setattr(
        maintenance,
        'ensure_canonical_apply_control_state',
        lambda uid, **_: MemoryControlState(uid=uid, head_commit_id='head0', account_generation=1, source_generation=1),
    )
    monkeypatch.setattr(
        maintenance,
        'run_canonical_short_term_ttl_lifecycle',
        lambda uid, **_: maintenance.CanonicalShortTermLifecycleReport(uid=uid),
    )
    monkeypatch.setattr(
        maintenance,
        'run_canonical_consolidation',
        lambda uid, **_: ConsolidationReport(
            uid=uid,
            pending_count=1,
            watermark_blocked=blocked,
            decisions_applied=0 if blocked else 1,
            errors=['invoke_failed:TimeoutError'] if blocked else [],
        ),
    )
    monkeypatch.setattr(
        maintenance,
        'run_canonical_memory_outbox_worker_tick',
        lambda **_: {'leased_count': 1, 'delivered_count': 1},
    )
    monkeypatch.setattr(recurrence, 'drain_recurrence_inbox_for_maintenance', lambda *args, **kwargs: 0)
    report = worker.run_cycle(
        db_client=mock.sentinel.db,
        config=worker.Config(poll_seconds=5, uid_limit=1),
        inventory=lambda db, limit: ('uid-a',),
    )
    assert report.delivered_count == 2
    assert report.succeeded is not blocked
    assert report.decisions_applied == (0 if blocked else 1)


@pytest.mark.parametrize('storage_failure', [False, True])
def test_recurrence_receipt_is_durable_before_watermark_and_consumed_afterward(monkeypatch, storage_failure):
    signal = _recurrence_signal(distinct_days=3)
    pending = {}
    completed = []
    watermark = []

    def enqueue(uid, signal, **kwargs):
        if storage_failure:
            raise RuntimeError('inbox unavailable')
        receipt = mock.Mock(receipt_id='receipt', signal=signal, account_generation=1)
        pending[receipt.receipt_id] = receipt
        return receipt

    def complete(uid, receipt_id, **kwargs):
        completed.append(pending.pop(receipt_id).signal.signal_id)

    def consolidate(uid, *, recurrence_signal_sink=None, **kwargs):
        if recurrence_signal_sink is not None:
            recurrence_signal_sink(uid, [signal], firestore_client=mock.sentinel.db)
        watermark.append('advanced')
        return ConsolidationReport(uid=uid, recurrence_signals=[signal])

    monkeypatch.setattr(
        recurrence.workstreams_db, 'get_task_workflow_control', lambda *args, **kwargs: mock.Mock(account_generation=1)
    )
    monkeypatch.setattr(
        recurrence,
        'persist_recurrence_signals_for_maintenance',
        partial(recurrence.persist_recurrence_signals_for_maintenance, enqueue=enqueue),
    )
    monkeypatch.setattr(
        recurrence,
        'drain_recurrence_inbox_for_maintenance',
        partial(
            recurrence.drain_recurrence_inbox_for_maintenance,
            list_pending=lambda *args, **kwargs: list(pending.values()),
            complete=complete,
        ),
    )
    monkeypatch.setattr(
        recurrence,
        'consume_recurrence_signal',
        lambda *args, **kwargs: mock.Mock(outcome=recurrence.RecurrenceOutcomeKind.candidate_created),
    )
    monkeypatch.setattr(
        maintenance,
        'ensure_canonical_apply_control_state',
        lambda uid, **kwargs: MemoryControlState(
            uid=uid, head_commit_id='head0', account_generation=1, source_generation=1
        ),
    )
    monkeypatch.setattr(
        maintenance,
        'run_canonical_short_term_ttl_lifecycle',
        lambda uid, **kwargs: maintenance.CanonicalShortTermLifecycleReport(uid=uid),
    )
    monkeypatch.setattr(maintenance, 'run_canonical_consolidation', consolidate)
    monkeypatch.setattr(maintenance, 'run_canonical_memory_outbox_worker_tick', lambda **kwargs: {})
    report = worker.run_cycle(
        db_client=mock.sentinel.db,
        config=worker.Config(poll_seconds=5, uid_limit=1),
        inventory=lambda db, limit: ('uid-a',),
    )
    assert completed == ([] if storage_failure else [signal.signal_id])
    assert watermark == ([] if storage_failure else ['advanced'])
    assert report.succeeded is not storage_failure
    assert not pending


def test_invalid_user_state_does_not_starve_later_users():
    processed = []

    def drain(uid, **_):
        if uid == 'invalid-user':
            raise ValueError('invalid canonical item')
        processed.append(uid)
        return {'delivered_count': 1, 'decisions_applied': 1}

    report = worker.run_cycle(
        db_client=mock.sentinel.db,
        config=worker.Config(poll_seconds=5, uid_limit=3),
        inventory=lambda db, limit: ('first-user', 'invalid-user', 'last-user'),
        drain=drain,
    )
    assert processed == ['first-user', 'last-user']
    assert report.user_count == 3
    assert report.error_count == 1
    assert report.delivered_count == report.decisions_applied == 2
    assert not report.succeeded


def test_cycle_drains_every_admitted_uid_and_reports_success():
    observed = datetime(2026, 9, 5, tzinfo=timezone.utc)
    drain = mock.Mock(
        side_effect=(
            {'delivered_count': 2, 'errors': []},
            {'delivered_count': 1, 'errors': []},
        )
    )
    report = worker.run_cycle(
        db_client=mock.sentinel.db,
        config=worker.Config(poll_seconds=5, uid_limit=2),
        inventory=lambda db, limit: ('uid-a', 'uid-b'),
        drain=drain,
        now=observed,
        run_id='cycle-a',
    )

    assert report == worker.CycleReport(
        user_count=2,
        delivered_count=3,
        retryable_failure_count=0,
        dead_letter_count=0,
        ack_failed_count=0,
        error_count=0,
    )
    assert report.succeeded is True
    assert [call.args[0] for call in drain.call_args_list] == ['uid-a', 'uid-b']


def test_cycle_keeps_provider_failure_visible_and_continues_the_bounded_page():
    drain = mock.Mock(
        side_effect=(
            {
                'delivered_count': 0,
                'retryable_failure_count': 1,
                'dead_letter_count': 0,
                'ack_failed_count': 0,
                'errors': [{'stage': 'process', 'code': 'vector_upsert_failed'}],
            },
            {'delivered_count': 2, 'errors': []},
        )
    )
    report = worker.run_cycle(
        db_client=mock.sentinel.db,
        config=worker.Config(poll_seconds=5, uid_limit=2),
        inventory=lambda db, limit: ('uid-failed', 'uid-healthy'),
        drain=drain,
        run_id='cycle-failure',
    )

    assert report.delivered_count == 2
    assert report.retryable_failure_count == 1
    assert report.error_count == 1
    assert report.succeeded is False
    assert drain.call_count == 2


@pytest.mark.parametrize(
    ('environment', 'message'),
    [
        ({'MEMORY_OUTBOX_POLL_SECONDS': '0'}, 'POLL_SECONDS'),
        ({'MEMORY_OUTBOX_POLL_SECONDS': 'slow'}, 'POLL_SECONDS'),
        ({'MEMORY_OUTBOX_UID_LIMIT': '0'}, 'UID_LIMIT'),
        ({'MEMORY_OUTBOX_UID_LIMIT': 'many'}, 'UID_LIMIT'),
    ],
)
def test_invalid_runtime_bounds_fail_before_work(environment, message):
    with mock.patch.dict(os.environ, environment, clear=True), pytest.raises(ProfileError, match=message):
        worker.Config.from_env()


def test_once_returns_nonzero_when_the_existing_owner_reports_a_retry():
    failed = worker.CycleReport(1, 0, 1, 0, 0, 1)
    with mock.patch.object(worker, 'bootstrap'), mock.patch.object(worker, 'run_cycle', return_value=failed):
        assert worker.run(['--once']) == 1


def test_supervised_loop_survives_one_inventory_failure_and_waits_before_retry(caplog):
    class StopAfterWait:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            assert seconds == 3
            self.stopped = True

    cycle = mock.Mock(side_effect=RuntimeError('synthetic provider body must not be logged'))
    worker.run_loop(
        db_client=mock.sentinel.db,
        config=worker.Config(poll_seconds=3, uid_limit=1),
        stop=StopAfterWait(),
        cycle=cycle,
    )
    assert cycle.call_count == 1
    assert 'RuntimeError' in caplog.text
    assert 'synthetic provider body' not in caplog.text
