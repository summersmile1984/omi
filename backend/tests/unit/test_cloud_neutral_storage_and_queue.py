import json
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from utils import cloud_tasks, cloud_tasks_redis
from utils.other import storage_minio


def _request(headers=None):
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    return Request({'type': 'http', 'headers': raw_headers})


def test_minio_client_reads_runtime_configuration(monkeypatch):
    clients = []

    def _client(service, **kwargs):
        clients.append((service, kwargs))
        return MagicMock()

    monkeypatch.setattr(storage_minio.boto3, 'client', _client)
    monkeypatch.setattr(storage_minio, '_client', None)
    monkeypatch.setattr(storage_minio, '_client_config', None)
    monkeypatch.setenv('MINIO_ENDPOINT', 'http://minio-one:9000')
    storage_minio.get_minio_client()
    monkeypatch.setenv('MINIO_ENDPOINT', 'http://minio-two:9000')
    storage_minio.get_minio_client()

    assert [config['endpoint_url'] for _, config in clients] == [
        'http://minio-one:9000',
        'http://minio-two:9000',
    ]


def test_redis_queue_deduplicates_and_uses_runtime_prefix(monkeypatch):
    redis_client = MagicMock()
    redis_client.sadd.side_effect = [1, 0]
    monkeypatch.setattr(cloud_tasks_redis, '_r', lambda: redis_client)
    monkeypatch.setenv('QUEUE_REDIS_PREFIX', 'test:queue')
    payload = {'job_id': 'job-1'}

    cloud_tasks_redis.enqueue_sync_job(payload)
    cloud_tasks_redis.enqueue_sync_job(payload)

    redis_client.sadd.assert_called_with('test:queue:sync:names', 'job-1')
    redis_client.rpush.assert_called_once_with(
        'test:queue:sync',
        json.dumps({'task_id': 'job-1', 'payload': payload}),
    )


def test_account_deletion_redis_payload_matches_handler_contract(monkeypatch):
    enqueue = MagicMock()
    monkeypatch.setattr(cloud_tasks_redis, '_enqueue', enqueue)

    cloud_tasks_redis.enqueue_account_deletion_wipe('wipe-123')

    enqueue.assert_called_once_with(
        'omi:queue:account-deletion',
        'wipe-wipe-123',
        {'job_id': 'wipe-123'},
    )


def test_redis_worker_auth_fails_closed(monkeypatch):
    monkeypatch.setenv('QUEUE_BACKEND', 'redis')
    monkeypatch.setenv('QUEUE_REDIS_WORKER_SECRET', 'expected-secret')

    with pytest.raises(cloud_tasks.HTTPException) as exc:
        cloud_tasks.verify_cloud_tasks_oidc(_request({'x-omi-queue-secret': 'wrong-secret'}))
    assert exc.value.status_code == 403

    assert cloud_tasks.verify_cloud_tasks_oidc(_request({'x-omi-queue-secret': 'expected-secret'})) == 0
