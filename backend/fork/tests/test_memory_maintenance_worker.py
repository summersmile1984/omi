from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest import mock

import pytest

from fork import memory_maintenance_worker as worker
from fork.patches import collect_memory_projection
from fork.profile import ProfileError


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


def test_projection_registry_contains_only_the_required_provider_seams():
    assert {patch.name for patch in collect_memory_projection()} == {
        'embedding.utils.llm.clients',
        'embedding.database.vector_db',
        'vector.qdrant-index',
        'provider.receipt-fence.database.vector_db',
        'provider.receipt-fence.utils.memory.atom_keyword_index',
    }


def test_production_drain_uses_the_existing_lease_and_projection_owner():
    observed = datetime(2026, 9, 5, tzinfo=timezone.utc)
    expected = {'delivered_count': 2, 'errors': []}
    with mock.patch('utils.memory.short_term_promotion._drain_canonical_outbox', return_value=expected) as drain:
        assert worker._production_drain('uid-a', db_client=mock.sentinel.db, run_id='cycle-a', now=observed) == expected
    drain.assert_called_once_with('uid-a', db_client=mock.sentinel.db, run_id='cycle-a', now=observed)


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
