"""Authenticated Redis attempts reach real worker termination policy, without live services."""

from collections import deque
import json

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest
from starlette.requests import Request

from fork.patches.queue import _route_worker_auth
from fork.queue_config import QUEUES


class Redis:
    def __init__(self, items):
        self.items = deque(items)
        self.parked = []

    def blpop(self, keys, timeout):
        if not self.items:
            raise KeyboardInterrupt
        return keys[0], self.items.popleft()

    def rpush(self, key, value):
        (self.parked if key.endswith(':dead-letter') else self.items).append(value)


def configure(monkeypatch, queue):
    monkeypatch.setenv('QUEUE_BACKEND', 'redis')
    monkeypatch.setenv('SYNC_TASKS_MAX_ATTEMPTS', '3')
    monkeypatch.setenv(queue.attempts_env, '3')
    monkeypatch.setenv(queue.handler_env, 'http://backend:8080' + queue.path)
    monkeypatch.setenv(queue.secret_env, 'synthetic-test-route-secret-' + queue.name)
    monkeypatch.setenv('QUEUE_REDIS_WORKER_SECRET', 'synthetic-test-route-secret-' + queue.name)


def request(queue, count=None, *, valid=True):
    headers = [(b'x-omi-queue-secret', ('synthetic-test-route-secret-' + queue.name if valid else 'wrong').encode())]
    if count is not None:
        headers.append((b'x-omi-queue-retry-count', count.encode()))
    return Request({'type': 'http', 'path': queue.path, 'headers': headers, 'scheme': 'http'})


@pytest.mark.parametrize('queue', QUEUES, ids=lambda queue: queue.name)
def test_retry_headers_are_authenticated_scoped_bounded_and_legacy_starts_at_zero(monkeypatch, queue):
    from utils import cloud_tasks

    configure(monkeypatch, queue)
    monkeypatch.setattr(cloud_tasks, '_verify_redis_worker', _route_worker_auth(None))
    owner = (
        cloud_tasks.verify_account_deletion_cloud_tasks_oidc
        if queue.name == 'account-deletion'
        else (
            cloud_tasks.verify_listen_finalization_cloud_tasks_oidc
            if queue.name == 'finalization'
            else cloud_tasks.verify_cloud_tasks_oidc
        )
    )
    for count, expected in ((None, 0), ('0', 0), ('2', 2)):
        result = owner(request(queue, count))
        if queue.name == 'account-deletion':
            assert result.audience == 'account_deletion'
            result = result.retry_count
        assert result == expected
    for count in ('-1', '1.0', ' 1', '3', '1000', '\u0661'):
        with pytest.raises(HTTPException) as error:
            owner(request(queue, count))
        assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        owner(request(queue, '2', valid=False))
    assert error.value.status_code == 403


@pytest.mark.parametrize('queue', QUEUES, ids=lambda queue: queue.name)
@pytest.mark.parametrize('failure', ['http', 'transport'])
def test_worker_retries_all_four_queues_with_persisted_counts_and_parks_exhaustion(monkeypatch, queue, failure):
    from utils import cloud_tasks_redis as worker

    configure(monkeypatch, queue)
    redis = Redis([json.dumps({'task_id': 'existing-task', 'payload': {'job_id': 'existing-job'}})])
    calls = []

    def post(url, *, json, headers, timeout):
        assert url.endswith(queue.path) and json == {'job_id': 'existing-job'}
        assert headers['X-Omi-Queue-Secret'] == 'synthetic-test-route-secret-' + queue.name
        calls.append(headers['X-Omi-Queue-Retry-Count'])
        if failure == 'transport':
            raise httpx.ReadTimeout('controlled transport fault')
        return httpx.Response(503)

    monkeypatch.setattr(worker, '_r', lambda: redis)
    monkeypatch.setattr(worker.httpx, 'post', post)
    monkeypatch.setattr(worker.time, 'sleep', lambda _: None)
    with pytest.raises(KeyboardInterrupt):
        worker._worker(queue.name)
    assert calls == ['0', '1', '2']
    assert len(redis.parked) == 1 and json.loads(redis.parked[0])['retry_count'] == 3


def test_actual_finalizer_handler_receives_final_attempt_and_terminal_ack_stops_retries(monkeypatch):
    from routers import conversation_finalization as route
    from utils import cloud_tasks, cloud_tasks_redis as worker

    queue = next(queue for queue in QUEUES if queue.name == 'finalization')
    configure(monkeypatch, queue)
    monkeypatch.setattr(cloud_tasks, '_verify_redis_worker', _route_worker_auth(None))
    redis = Redis(
        [json.dumps({'task_id': 'existing-finalizer', 'payload': {'job_id': 'job', 'dispatch_generation': 2}})]
    )
    attempts, terminal, retryable = [], [], []

    async def immediate(executor, function, *args, **kwargs):
        return function(*args, **kwargs)

    async def fail(uid, conversation_id, **kwargs):
        attempts.append(kwargs['final_attempt'])
        raise route.ConversationFinalizationError('controlled_provider_failure')

    monkeypatch.setattr(route, 'run_blocking', immediate)
    monkeypatch.setattr(route, 'try_acquire_job_run_lock', lambda *args: 'lock')
    monkeypatch.setattr(route, 'release_job_run_lock', lambda *args: None)
    monkeypatch.setattr(route, 'should_skip_background_account_mutation', lambda *args: False)
    monkeypatch.setattr(route.jobs_db, 'claim_finalization_job', lambda *args: {'status': 'claimed', 'lease_epoch': 4})
    monkeypatch.setattr(
        route.jobs_db, 'get_finalization_job', lambda *args: {'uid': 'old-user', 'conversation_id': 'conv'}
    )
    monkeypatch.setattr(route.jobs_db, 'mark_finalization_retryable', lambda *args: retryable.append(args))
    monkeypatch.setattr(route, 'final_attempt_failed', lambda *args: terminal.append(args) or True)
    monkeypatch.setattr(route, 'finalize_persisted_conversation', fail)
    app = FastAPI()
    app.include_router(route.router)
    with TestClient(app) as client:
        monkeypatch.setattr(worker, '_r', lambda: redis)
        monkeypatch.setattr(worker.httpx, 'post', lambda url, **kwargs: client.post(queue.path, **kwargs))
        monkeypatch.setattr(worker.time, 'sleep', lambda _: None)
        with pytest.raises(KeyboardInterrupt):
            worker._worker(queue.name)
    assert attempts == [False, False, True]
    assert len(retryable) == 2 and terminal == [('job', 2, 4, 3)]
    assert not redis.parked and not redis.items


def test_invalid_envelope_is_retained_without_dispatch_or_retry_loop(monkeypatch):
    from utils import cloud_tasks_redis as worker

    queue = QUEUES[0]
    configure(monkeypatch, queue)
    items = ['not-json', '[]', json.dumps({'task_id': 'old', 'payload': {}, 'retry_count': True})]
    redis = Redis(items)
    monkeypatch.setattr(worker, '_r', lambda: redis)
    monkeypatch.setattr(worker.httpx, 'post', lambda *args, **kwargs: pytest.fail('invalid envelope dispatched'))
    with pytest.raises(KeyboardInterrupt):
        worker._worker(queue.name)
    assert redis.parked == items


@pytest.mark.parametrize('status', [301, 400, 401, 403])
def test_rejected_delivery_is_parked_and_lease_conflict_retries_with_existing_count(monkeypatch, status):
    from utils import cloud_tasks_redis as worker

    queue = QUEUES[-1]
    configure(monkeypatch, queue)
    redis = Redis([json.dumps({'task_id': 'existing', 'payload': {'job_id': 'job'}, 'retry_count': 1})])
    seen = []

    def post(url, **kwargs):
        seen.append(kwargs['headers']['X-Omi-Queue-Retry-Count'])
        return httpx.Response(409 if len(seen) == 1 else status)

    monkeypatch.setattr(worker, '_r', lambda: redis)
    monkeypatch.setattr(worker.httpx, 'post', post)
    monkeypatch.setattr(worker.time, 'sleep', lambda _: None)
    with pytest.raises(KeyboardInterrupt):
        worker._worker(queue.name)
    assert seen == ['1', '2']
    assert len(redis.parked) == 1
    assert json.loads(redis.parked[0])['delivery_failure'] == f'http_{status}'
