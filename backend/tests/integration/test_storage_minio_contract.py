import os
import uuid

import httpx
import pytest

from utils.other import storage_minio


@pytest.fixture
def real_minio():
    endpoint = os.getenv('MINIO_CONTRACT_ENDPOINT', '').strip()
    if not endpoint:
        pytest.skip('MINIO_CONTRACT_ENDPOINT is not configured')
    access_key = os.getenv('MINIO_CONTRACT_ACCESS_KEY', 'minioadmin')
    secret_key = os.getenv('MINIO_CONTRACT_SECRET_KEY', 'minioadmin')
    client = storage_minio._MinioClient(
        (endpoint, access_key, secret_key, 'us-east-1', endpoint.startswith('https://'), endpoint)
    )
    bucket_name = f'omi-contract-{uuid.uuid4().hex}'
    bucket = client.bucket(bucket_name)
    yield client, bucket
    objects = list(bucket.list_blobs())
    for blob in objects:
        blob.delete()
    client._s3.delete_bucket(Bucket=bucket_name)


def test_real_minio_blob_bucket_and_public_policy_contract(real_minio, tmp_path):
    client, bucket = real_minio
    source = bucket.blob('source/a b.json')
    source.metadata = {'generation': '7'}
    source.cache_control = 'public, no-cache'
    source.upload_from_string(b'{"ok":true}', content_type='application/json')

    source.reload()
    assert source.size == 11
    assert source.metadata == {'generation': '7'}
    assert source.download_as_bytes() == b'{"ok":true}'
    assert source.generate_signed_url(expiration=60, method='GET').startswith(client._public_endpoint)

    source.make_public()
    policy = client._s3.get_bucket_policy(Bucket=bucket.name)['Policy']
    assert 'arn:aws:s3:::' + bucket.name + '/source/a b.json' in policy
    response = httpx.get(source.public_url)
    assert response.status_code == 200
    assert response.content == b'{"ok":true}'

    copied = bucket.copy_blob(source, bucket, 'copy/a.json')
    copied.reload()
    assert copied.size == 11
    assert sorted((blob.name, blob.size) for blob in bucket.list_blobs()) == [
        ('copy/a.json', 11),
        ('source/a b.json', 11),
    ]

    output = tmp_path / 'download.json'
    copied.download_to_filename(str(output))
    assert output.read_bytes() == b'{"ok":true}'
