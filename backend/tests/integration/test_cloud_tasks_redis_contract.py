import os
import uuid
from types import SimpleNamespace

import pytest
import redis

from utils import cloud_tasks_redis


@pytest.fixture
def real_queue(monkeypatch):
    url = os.getenv('QUEUE_REDIS_CONTRACT_URL', '').strip()
    if not url:
        pytest.skip('QUEUE_REDIS_CONTRACT_URL is not configured')
    client = redis.Redis.from_url(url, decode_responses=True)
    client.ping()
    prefix = f'contract:queue:{uuid.uuid4().hex}'
    monkeypatch.setattr(cloud_tasks_redis, '_r', lambda: client)
    monkeypatch.setenv('QUEUE_REDIS_PREFIX', prefix)
    monkeypatch.setenv('SYNC_TASKS_HANDLER_URL', 'http://contract/sync')
    monkeypatch.setenv('QUEUE_REDIS_SYNC_WORKER_SECRET', 'contract-secret')
    monkeypatch.setenv('QUEUE_REDIS_RETRY_BASE_SECONDS', '0.001')
    yield client, prefix
    keys = list(client.scan_iter(f'{prefix}:*'))
    if keys:
        client.delete(*keys)


def test_real_redis_enqueue_lease_retry_ack_contract(real_queue):
    client, prefix = real_queue
    cloud_tasks_redis._enqueue(f'{prefix}:sync', 'job-1', {'job_id': 'job-1'}, now_ms=1000)
    seen_retry_counts = []

    def post(*args, **kwargs):
        seen_retry_counts.append(kwargs['headers']['X-CloudTasks-TaskRetryCount'])
        return SimpleNamespace(status_code=409 if len(seen_retry_counts) == 1 else 200)

    cloud_tasks_redis._process_one('sync', http_post=post, now_ms=1000)
    cloud_tasks_redis._process_one('sync', http_post=post, now_ms=1001)

    assert seen_retry_counts == ['0', '1']
    assert client.zcard(f'{prefix}:sync:ready') == 0
    assert client.zcard(f'{prefix}:sync:pending') == 0
    assert list(client.scan_iter(f'{prefix}:sync:task:*')) == []
