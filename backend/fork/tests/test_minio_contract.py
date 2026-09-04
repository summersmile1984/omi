"""Real boto signing and controlled S3 error/surface tests without live services."""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from botocore.exceptions import ClientError
import pytest

from fork.storage_minio import Config, _MinioClient


@pytest.fixture
def client():
    result = _MinioClient(Config('http://internal:9000', 'https://objects.example', 'synthetic', 'synthetic-secret'))
    result._s3 = mock.Mock()
    return result


def test_stream_metadata_list_size_and_public_signing(client):
    bucket = client.bucket('private-audio')
    blob = bucket.blob('owner/audio.bin')
    blob.metadata = {'expires_at': 123}
    blob.cache_control = 'private, max-age=60'

    def upload(stream, *args, **kwargs):
        assert stream.read() == b'hello'
        assert kwargs['ExtraArgs']['Metadata'] == {'expires_at': '123'}

    client._s3.upload_fileobj.side_effect = upload
    with blob.open('wb', content_type='audio/pcm') as stream:
        stream.write(b'hello')
    client._s3.get_paginator.return_value.paginate.return_value = [{'Contents': [{'Key': blob.name, 'Size': 5}]}]
    assert next(bucket.list_blobs()).size == 5
    parsed = urlsplit(blob.generate_signed_url(version='v4', expiration=timedelta(minutes=15), method='GET'))
    assert parsed.netloc == 'objects.example' and parsed.path == '/private-audio/owner/audio.bin'
    assert parse_qs(parsed.query)['X-Amz-Expires'] == ['900']
    with pytest.raises(ValueError):
        blob.generate_signed_url(method='PUT')
    with pytest.raises(ValueError):
        blob.make_public()


def test_only_authoritative_not_found_counts_as_absence(client):
    blob = client.bucket('audio').blob('owner/item')
    client._s3.head_object.side_effect = ClientError({'Error': {'Code': '404'}}, 'HeadObject')
    assert not blob.exists()
    client._s3.head_object.side_effect = ClientError({'Error': {'Code': 'AccessDenied'}}, 'HeadObject')
    with pytest.raises(ClientError):
        blob.exists()
    client._s3.head_bucket.side_effect = ClientError({'Error': {'Code': 'AccessDenied'}}, 'HeadBucket')
    with pytest.raises(ClientError):
        client.bucket('denied')
    client._s3.create_bucket.assert_not_called()


def test_interrupted_stream_does_not_publish_partial_object(client):
    blob = client.bucket('audio').blob('owner/item')
    with pytest.raises(RuntimeError):
        with blob.open('wb') as stream:
            stream.write(b'partial')
            raise RuntimeError('producer failed')
    client._s3.upload_fileobj.assert_not_called()


def test_signing_cache_is_scoped_to_bucket_and_origin(client, monkeypatch):
    from fork.provider_objects import signed_url
    from database import redis_db

    cache = {}
    monkeypatch.setattr(redis_db, 'get_cached_signed_url', lambda key: cache.get(key))
    monkeypatch.setattr(redis_db, 'cache_signed_url', lambda key, value, ttl: cache.update({key: value}))
    a = signed_url(client.bucket('a').blob('same-name'), 5)
    b = signed_url(client.bucket('b').blob('same-name'), 5)
    assert a != b and len(cache) == 2
    assert signed_url(client.bucket('a').blob('same-name'), 5) == a
