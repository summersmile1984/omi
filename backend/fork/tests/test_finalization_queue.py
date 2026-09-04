"""Real lifecycle and captured dispatch owners over a controlled Redis boundary."""

import json

import pytest

from fork import finalization_queue
from fork.patches.queue import _finalization_patches
from fork.registry import build_registry


@pytest.fixture
def installed(monkeypatch):
    queue = finalization_queue.FINALIZATION
    monkeypatch.setenv(queue.handler_env, 'http://backend:8080' + queue.path)
    monkeypatch.setenv(queue.secret_env, 'synthetic-finalizer-credential-123456')
    for key in ('SYNC_TASKS_PROJECT', 'SYNC_TASKS_LOCATION', 'LISTEN_FINALIZATION_TASKS_QUEUE'):
        monkeypatch.delenv(key, raising=False)
    patches = _finalization_patches()
    originals = [(patch.target()[0], patch.attribute, patch.target()[1]) for patch in patches]
    try:
        build_registry(patches).apply({'target': 'self_hosted', 'data_plane': {'queue': 'redis'}})
        yield
    finally:
        for owner, attribute, value in originals:
            setattr(owner, attribute, value)


def test_legacy_job_duplicate_and_new_generation_reach_the_pg_lease_owner(installed, monkeypatch):
    from utils import cloud_tasks, cloud_tasks_redis
    from utils.conversations import lifecycle
    from services import conversation_finalization as reconciler
    from routers.listen import conversations as listen

    published = []

    class Redis:
        # No name-set API: publication is one atomic Redis command. Duplicate
        # deliveries and newer generations reach the durable PG claim owner.
        def rpush(self, key, value):
            published.append((key, json.loads(value)))

    monkeypatch.setattr(cloud_tasks_redis, '_r', lambda: Redis())
    for owner in (cloud_tasks, lifecycle, reconciler, listen):
        assert owner.is_listen_finalization_dispatch_enabled()
    assert cloud_tasks.is_listen_finalization_dispatch_configured()
    for owner, generation in ((cloud_tasks, 1), (lifecycle, 1), (reconciler, 2), (cloud_tasks_redis, 2)):
        owner.enqueue_listen_finalization_job('existing-unmigrated-job', generation)
    assert [item[1]['payload'] for item in published] == [
        {'job_id': 'existing-unmigrated-job', 'dispatch_generation': generation} for generation in (1, 1, 2, 2)
    ]
    assert len({item[1]['task_id'] for item in published}) == 2


def test_rest_admission_uses_redis_configuration_and_outage_retains_durable_intent(installed, monkeypatch):
    from utils import cloud_tasks_redis
    from utils.conversations import lifecycle

    accepted = []

    def intent(*args, **kwargs):
        accepted.append(args)
        return {
            'job_id': 'legacy-job',
            'status': 'queued',
            'dispatch_generation': 2,
            'requires_byok': False,
            'created': False,
        }

    class Offline:
        def rpush(self, *args):
            raise ConnectionError('controlled Redis outage')

    monkeypatch.setattr(lifecycle.jobs_db, 'create_or_get_finalization_intent', intent)
    monkeypatch.setattr(cloud_tasks_redis, '_r', lambda: Offline())
    result = lifecycle.request_finalization(
        'existing-user', 'existing-conversation', has_byok_keys=False, require_cloud_tasks=True
    )
    assert result['route'] == 'queued' and result['dispatch_generation'] == 2
    assert len(accepted) == 1
    monkeypatch.setenv(finalization_queue.FINALIZATION.secret_env, 'short')
    with pytest.raises(lifecycle.FinalizationDispatchUnavailable):
        lifecycle.request_finalization(
            'existing-user', 'existing-conversation', has_byok_keys=False, require_cloud_tasks=True
        )
    assert len(accepted) == 1  # Invalid authority rejects before creating an intent.
    assert lifecycle.is_listen_finalization_dispatch_enabled()  # Never falls back to inline.


def test_omi_cloud_keeps_original_dispatch_admission():
    import utils.cloud_tasks as cloud_tasks

    configured = cloud_tasks.is_listen_finalization_dispatch_configured
    build_registry(_finalization_patches()).apply({'target': 'omi_cloud', 'data_plane': {'queue': 'cloud_tasks'}})
    assert cloud_tasks.is_listen_finalization_dispatch_configured is configured
