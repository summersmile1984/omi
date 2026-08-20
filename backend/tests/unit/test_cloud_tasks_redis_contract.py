import json
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import fakeredis
import pytest
from starlette.requests import Request

from utils import cloud_tasks, cloud_tasks_redis


def _load_live_replacement_smoke():
    path = Path(__file__).resolve().parents[3] / 'deploy' / 'self-host' / 'live-replacement-smoke.py'
    spec = importlib.util.spec_from_file_location('self_host_live_replacement_smoke', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(headers=None, *, path='/v2/sync-jobs/run'):
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    return Request({'type': 'http', 'headers': raw_headers, 'path': path})


@pytest.fixture
def queue(monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(cloud_tasks_redis, '_r', lambda: client)
    monkeypatch.setenv('QUEUE_REDIS_PREFIX', 'test:queue')
    monkeypatch.setenv('QUEUE_REDIS_RETRY_BASE_SECONDS', '1')
    monkeypatch.setenv('QUEUE_REDIS_RETRY_MAX_SECONDS', '4')
    monkeypatch.setenv('QUEUE_REDIS_LEASE_SECONDS', '10')
    for queue_name, handler_env in cloud_tasks_redis.HANDLER_URL_ENV.items():
        monkeypatch.setenv(handler_env, f'http://worker/{queue_name}')
        monkeypatch.setenv(cloud_tasks_redis.WORKER_SECRET_ENV[queue_name], f'{queue_name}-secret')
    return client


def test_named_enqueue_is_atomic_under_contention(queue):
    def enqueue():
        return cloud_tasks_redis._enqueue('test:queue:sync', 'job-1', {'job_id': 'job-1'}, now_ms=1000)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: enqueue(), range(24)))

    assert results.count(True) == 1
    assert queue.zcard('test:queue:sync:ready') == 1
    assert len(list(queue.scan_iter('test:queue:sync:task:*'))) == 1


def test_expired_pending_lease_is_recovered_after_worker_crash(queue, monkeypatch):
    monkeypatch.setenv('QUEUE_REDIS_LEASE_SECONDS', '1')
    cloud_tasks_redis._enqueue('test:queue:sync', 'job-1', {'job_id': 'job-1'}, now_ms=1000)

    first = cloud_tasks_redis._claim_task('sync', now_ms=1000)
    assert first is not None
    assert cloud_tasks_redis._claim_task('sync', now_ms=1999) is None

    recovered = cloud_tasks_redis._claim_task('sync', now_ms=2000)
    assert recovered is not None
    assert recovered.task_id == 'job-1'
    assert recovered.lease_owner != first.lease_owner
    assert cloud_tasks_redis._ack_task(first) is False
    assert cloud_tasks_redis._ack_task(recovered) is True


@pytest.mark.parametrize('status_code', [409, 429, 500, 503])
def test_retryable_http_statuses_use_exponential_backoff_and_retry_header(queue, status_code):
    cloud_tasks_redis._enqueue('test:queue:sync', 'job-1', {'job_id': 'job-1'}, now_ms=1000)
    headers = []

    def post(*args, **kwargs):
        headers.append(kwargs['headers'])
        return SimpleNamespace(status_code=status_code)

    assert cloud_tasks_redis._process_one('sync', http_post=post, now_ms=1000)
    assert headers[0]['X-CloudTasks-TaskRetryCount'] == '0'
    assert cloud_tasks_redis._claim_task('sync', now_ms=1999) is None
    retried = cloud_tasks_redis._claim_task('sync', now_ms=2000)
    assert retried is not None
    assert retried.retry_count == 1


def test_retry_budget_exhaustion_moves_task_to_dlq(queue, monkeypatch):
    monkeypatch.setenv('SYNC_TASKS_MAX_ATTEMPTS', '3')
    cloud_tasks_redis._enqueue('test:queue:sync', 'job-1', {'job_id': 'job-1'}, now_ms=0)
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(kwargs['headers']['X-CloudTasks-TaskRetryCount'])
        return SimpleNamespace(status_code=503)

    cloud_tasks_redis._process_one('sync', http_post=fail, now_ms=0)
    cloud_tasks_redis._process_one('sync', http_post=fail, now_ms=1000)
    cloud_tasks_redis._process_one('sync', http_post=fail, now_ms=3000)

    assert attempts == ['0', '1', '2']
    assert queue.zcard('test:queue:sync:ready') == 0
    assert queue.zcard('test:queue:sync:pending') == 0
    dead_letter = json.loads(queue.lindex('test:queue:sync:dlq', 0))
    assert dead_letter['task_id'] == 'job-1'
    assert dead_letter['retry_count'] == 3
    assert dead_letter['last_error'] == 'http_503'


def test_transport_fault_is_retried_but_nonretryable_4xx_is_dead_lettered(queue):
    cloud_tasks_redis._enqueue('test:queue:sync', 'network', {'job_id': 'network'}, now_ms=0)

    def disconnect(*args, **kwargs):
        raise RuntimeError('connection dropped')

    cloud_tasks_redis._process_one('sync', http_post=disconnect, now_ms=0)
    retried = cloud_tasks_redis._claim_task('sync', now_ms=1000)
    assert retried is not None
    cloud_tasks_redis._ack_task(retried)

    cloud_tasks_redis._enqueue('test:queue:sync', 'bad-request', {'job_id': 'bad-request'}, now_ms=2000)
    cloud_tasks_redis._process_one(
        'sync', http_post=lambda *args, **kwargs: SimpleNamespace(status_code=400), now_ms=2000
    )
    assert queue.llen('test:queue:sync:dlq') == 1


def test_success_ack_removes_dedupe_tombstone_and_allows_reenqueue(queue):
    payload = {'job_id': 'job-1'}
    assert cloud_tasks_redis._enqueue('test:queue:sync', 'job-1', payload, now_ms=0)
    cloud_tasks_redis._process_one('sync', http_post=lambda *args, **kwargs: SimpleNamespace(status_code=204), now_ms=0)
    assert cloud_tasks_redis._enqueue('test:queue:sync', 'job-1', payload, now_ms=1)


def test_live_smoke_cleanup_waits_for_delivery_then_removes_only_its_exact_retry(queue, monkeypatch):
    queue_key = 'test:queue:account-deletion'
    task_id = 'wipe-live-smoke-job'
    other_task_id = 'wipe-production-job'
    assert cloud_tasks_redis._enqueue(queue_key, task_id, {'job_id': 'live-smoke-job'}, now_ms=0)
    assert cloud_tasks_redis._enqueue(queue_key, other_task_id, {'job_id': 'production-job'}, now_ms=1)
    claimed = cloud_tasks_redis._claim_task('account-deletion', now_ms=0)
    assert claimed is not None and claimed.task_id == task_id
    live_smoke = _load_live_replacement_smoke()

    def finish_failed_delivery(_seconds):
        assert cloud_tasks_redis._fail_task(claimed, 'fixture_failure', retryable=True, now_ms=1) == 'retry'

    monkeypatch.setattr(live_smoke.time, 'sleep', finish_failed_delivery)

    assert live_smoke.stop_exact_deletion_task(queue, queue_key, claimed.token, claimed.task_key) is True
    assert not queue.exists(claimed.task_key)
    assert queue.zscore(f'{queue_key}:ready', claimed.token) is None
    assert queue.zscore(f'{queue_key}:pending', claimed.token) is None
    other_token = cloud_tasks_redis._task_token(other_task_id)
    assert queue.exists(cloud_tasks_redis._task_key(queue_key, other_token))
    assert queue.zscore(f'{queue_key}:ready', other_token) is not None


def test_finalization_task_identity_includes_dispatch_generation(queue):
    cloud_tasks_redis.enqueue_listen_finalization_job('job-1', 1)
    cloud_tasks_redis.enqueue_listen_finalization_job('job-1', 2)

    assert queue.zcard('test:queue:finalization:ready') == 2


def test_route_auth_is_queue_scoped_and_propagates_retry_count(monkeypatch):
    monkeypatch.setenv('QUEUE_BACKEND', 'redis')
    monkeypatch.setenv('QUEUE_REDIS_SYNC_WORKER_SECRET', 'sync-secret')
    monkeypatch.setenv('QUEUE_REDIS_AUDIO_MERGE_WORKER_SECRET', 'audio-secret')
    monkeypatch.setenv('QUEUE_REDIS_ACCOUNT_DELETION_WORKER_SECRET', 'delete-secret')

    assert (
        cloud_tasks.verify_cloud_tasks_oidc(
            _request(
                {
                    'x-omi-queue-name': 'sync',
                    'x-omi-queue-secret': 'sync-secret',
                    'x-cloudtasks-taskretrycount': '4',
                }
            )
        )
        == 4
    )
    with pytest.raises(cloud_tasks.HTTPException) as error:
        cloud_tasks.verify_cloud_tasks_oidc(
            _request({'x-omi-queue-name': 'account-deletion', 'x-omi-queue-secret': 'delete-secret'})
        )
    assert error.value.status_code == 403

    with pytest.raises(cloud_tasks.HTTPException) as error:
        cloud_tasks.verify_cloud_tasks_oidc(
            _request(
                {'x-omi-queue-name': 'audio-merge', 'x-omi-queue-secret': 'audio-secret'},
                path='/v2/sync-jobs/run',
            )
        )
    assert error.value.status_code == 403

    assert (
        cloud_tasks.verify_cloud_tasks_oidc(
            _request(
                {'x-omi-queue-name': 'audio-merge', 'x-omi-queue-secret': 'audio-secret'},
                path='/v2/audio-merge-jobs/run',
            )
        )
        == 0
    )


def test_production_redis_dispatch_starts_without_gcp_configuration(monkeypatch):
    gcp_names = (
        'SYNC_TASKS_PROJECT',
        'SYNC_TASKS_LOCATION',
        'SYNC_TASKS_INVOKER_SA',
        'SYNC_TASKS_HANDLER_URL',
    )
    for name in gcp_names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OMI_ENV_STAGE', 'prod')
    monkeypatch.setenv('QUEUE_BACKEND', 'redis')
    monkeypatch.setenv('ACCOUNT_DELETION_DISPATCH_MODE', 'cloud_tasks')
    monkeypatch.setenv('ACCOUNT_DELETION_HANDLER_URL', 'http://worker/account-deletion')
    monkeypatch.setenv('QUEUE_REDIS_ACCOUNT_DELETION_WORKER_SECRET', 'delete-secret')

    cloud_tasks.validate_account_deletion_dispatch_configuration()


def test_redis_finalization_configuration_does_not_require_gcp(monkeypatch):
    monkeypatch.setenv('QUEUE_BACKEND', 'redis')
    monkeypatch.setenv('LISTEN_FINALIZATION_DISPATCH_MODE', 'cloud_tasks')
    monkeypatch.setenv('LISTEN_FINALIZATION_TASKS_HANDLER_URL', 'http://worker/finalization')
    monkeypatch.setenv('QUEUE_REDIS_FINALIZATION_WORKER_SECRET', 'final-secret')
    monkeypatch.delenv('SYNC_TASKS_PROJECT', raising=False)
    monkeypatch.delenv('SYNC_TASKS_INVOKER_SA', raising=False)

    assert cloud_tasks.is_listen_finalization_dispatch_configured() is True


def test_all_worker_supervisor_fails_container_when_any_queue_worker_exits(monkeypatch):
    def worker(queue_name):
        if queue_name == 'account-deletion':
            return
        # Other daemon workers may remain blocked; the supervisor must still
        # fail immediately on the lost account-deletion consumer.
        import threading

        threading.Event().wait()

    monkeypatch.setattr(cloud_tasks_redis, '_worker', worker)

    with pytest.raises(RuntimeError, match='account-deletion exited unexpectedly'):
        cloud_tasks_redis._run_all_workers()


def test_all_worker_supervisor_preserves_child_exception(monkeypatch):
    def worker(queue_name):
        if queue_name == 'sync':
            raise ValueError('worker boom')
        import threading

        threading.Event().wait()

    monkeypatch.setattr(cloud_tasks_redis, '_worker', worker)

    with pytest.raises(RuntimeError, match='sync crashed') as error:
        cloud_tasks_redis._run_all_workers()
    assert isinstance(error.value.__cause__, ValueError)
