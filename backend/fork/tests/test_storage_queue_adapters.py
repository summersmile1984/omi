import json
from unittest.mock import MagicMock

from utils import cloud_tasks_redis
from fork import storage_minio


def test_minio_client_reads_runtime_configuration(monkeypatch):
    clients = []

    def _client(service, **kwargs):
        clients.append((service, kwargs))
        return MagicMock()

    monkeypatch.setattr(storage_minio.boto3, 'client', _client)
    monkeypatch.setattr(storage_minio, '_client', None)
    monkeypatch.setattr(storage_minio, '_client_config', None)
    monkeypatch.setenv('MINIO_ENDPOINT', 'http://minio-one:9000')
    monkeypatch.setenv('MINIO_PUBLIC_ENDPOINT', 'https://objects.example')
    monkeypatch.setenv('MINIO_ACCESS_KEY', 'synthetic-access')
    monkeypatch.setenv('MINIO_SECRET_KEY', 'synthetic-secret')
    monkeypatch.setenv('MINIO_REGION', 'us-east-1')
    first = storage_minio.get_minio_client()
    assert storage_minio.get_minio_client() is first
    monkeypatch.setenv('MINIO_ENDPOINT', 'http://minio-two:9000')
    second = storage_minio.get_minio_client()
    assert second is not first
    monkeypatch.setenv('MINIO_PUBLIC_ENDPOINT', 'https://new-objects.example')
    assert storage_minio.get_minio_client() is not second

    assert [config['endpoint_url'] for _, config in clients] == [
        'http://minio-one:9000',
        'https://objects.example',
        'http://minio-two:9000',
        'https://objects.example',
        'http://minio-two:9000',
        'https://new-objects.example',
    ]
    assert all(config['aws_access_key_id'] == 'synthetic-access' for _, config in clients)
    assert all(config['region_name'] == 'us-east-1' for _, config in clients)


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
