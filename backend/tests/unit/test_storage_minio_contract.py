import datetime
import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from google.cloud.exceptions import NotFound

from routers.listen import parity_pack_export
from utils.other import storage, storage_minio


def _client_error(code: str, operation: str = 'HeadObject') -> ClientError:
    return ClientError({'Error': {'Code': code, 'Message': code}}, operation)


def _blob(s3=None, public_s3=None):
    return storage_minio._MinioBlob(
        s3 or MagicMock(),
        public_s3 or MagicMock(),
        'https://objects.example.test/base',
        'bucket',
        'folder/a b.json',
    )


def test_blob_reload_exposes_size_metadata_and_content_headers():
    s3 = MagicMock()
    s3.head_object.return_value = {
        'ContentLength': 17,
        'Metadata': {'generation': '3'},
        'CacheControl': 'public, no-cache',
        'ContentType': 'application/json',
    }
    blob = _blob(s3=s3)

    blob.reload()

    assert blob.size == 17
    assert blob.metadata == {'generation': '3'}
    assert blob.cache_control == 'public, no-cache'
    assert blob.content_type == 'application/json'


def test_upload_preserves_metadata_cache_control_and_content_type(tmp_path):
    s3 = MagicMock()
    blob = _blob(s3=s3)
    blob.metadata = {'fingerprint': 'abc'}
    blob.cache_control = 'private, max-age=60'
    path = tmp_path / 'payload.json'
    path.write_bytes(b'{}')

    blob.upload_from_filename(str(path), content_type='application/json')

    s3.upload_file.assert_called_once_with(
        str(path),
        'bucket',
        'folder/a b.json',
        ExtraArgs={
            'ContentType': 'application/json',
            'CacheControl': 'private, max-age=60',
            'Metadata': {'fingerprint': 'abc'},
        },
    )
    assert blob.size == 2


def test_signed_url_accepts_gcs_signature_and_uses_public_endpoint_client():
    public_s3 = MagicMock()
    public_s3.generate_presigned_url.return_value = 'https://objects.example.test/signed'
    blob = _blob(public_s3=public_s3)

    result = blob.generate_signed_url(version='v4', expiration=datetime.timedelta(minutes=15), method='GET')

    assert result == 'https://objects.example.test/signed'
    public_s3.generate_presigned_url.assert_called_once_with(
        'get_object',
        Params={'Bucket': 'bucket', 'Key': 'folder/a b.json'},
        ExpiresIn=900,
        HttpMethod='GET',
    )


def test_public_url_and_supported_acl_use_minio_endpoint_without_gcs_hostname():
    s3 = MagicMock()
    blob = _blob(s3=s3)

    blob.make_public()

    assert blob.public_url == 'https://objects.example.test/base/bucket/folder/a%20b.json'
    assert 'storage.googleapis.com' not in blob.public_url
    s3.put_object_acl.assert_called_once_with(Bucket='bucket', Key='folder/a b.json', ACL='public-read')


def test_make_public_falls_back_to_object_scoped_bucket_policy_when_minio_rejects_acls():
    s3 = MagicMock()
    s3.put_object_acl.side_effect = _client_error('NotImplemented', 'PutObjectAcl')
    s3.get_bucket_policy.side_effect = _client_error('NoSuchBucketPolicy', 'GetBucketPolicy')
    blob = _blob(s3=s3)

    blob.make_public()

    policy = s3.put_bucket_policy.call_args.kwargs
    assert policy['Bucket'] == 'bucket'
    parsed = json.loads(policy['Policy'])
    assert parsed['Statement'][0]['Resource'] == ['arn:aws:s3:::bucket/folder/a b.json']


def test_not_found_is_distinct_from_provider_or_permission_failure():
    s3 = MagicMock()
    blob = _blob(s3=s3)
    s3.head_object.side_effect = _client_error('404')
    assert blob.exists() is False

    s3.head_object.side_effect = _client_error('AccessDenied')
    with pytest.raises(ClientError):
        blob.exists()

    s3.get_object.side_effect = _client_error('NoSuchKey', 'GetObject')
    with pytest.raises(NotFound):
        blob.download_as_bytes()

    s3.head_object.side_effect = _client_error('NoSuchKey')
    with pytest.raises(NotFound):
        blob.delete()

    s3.head_object.side_effect = _client_error('AccessDenied')
    with pytest.raises(ClientError):
        blob.delete()
    s3.delete_object.assert_not_called()


def test_list_blobs_exposes_sizes_and_copy_blob_matches_gcs_surface():
    s3 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{'Contents': [{'Key': 'a', 'Size': 4}, {'Key': 'b', 'Size': 8}]}]
    s3.get_paginator.return_value = paginator
    bucket = storage_minio._MinioBucket(s3, s3, 'http://minio.test', 'source')
    destination = storage_minio._MinioBucket(s3, s3, 'http://minio.test', 'destination')

    blobs = list(bucket.list_blobs(prefix='folder/'))
    copied = bucket.copy_blob(bucket.blob('a'), destination, 'copied/a')

    assert [(blob.name, blob.size) for blob in blobs] == [('a', 4), ('b', 8)]
    assert copied.name == 'copied/a'
    s3.copy_object.assert_called_once_with(
        CopySource={'Bucket': 'source', 'Key': 'a'}, Bucket='destination', Key='copied/a'
    )


def test_minio_client_uses_public_endpoint_for_urls_and_signing(monkeypatch):
    clients = []

    def create_client(service, **kwargs):
        client = MagicMock()
        clients.append((service, kwargs, client))
        return client

    monkeypatch.setattr(storage_minio.boto3, 'client', create_client)
    client = storage_minio._MinioClient(
        ('http://minio:9000', 'access', 'secret', 'us-east-1', False, 'https://cdn.example.test')
    )

    assert [item[1]['endpoint_url'] for item in clients] == [
        'http://minio:9000',
        'https://cdn.example.test',
    ]
    assert client.public_url('assets', 'icons/a b.png') == 'https://cdn.example.test/assets/icons/a%20b.png'
    assert client.object_name_from_url('assets', 'https://cdn.example.test/assets/icons/a%20b.png') == 'icons/a b.png'
    assert client.object_name_from_url('other', 'https://cdn.example.test/assets/icons/a%20b.png') is None


def test_cached_minio_client_reloads_mutable_runtime_configuration(monkeypatch):
    endpoints = []

    def create_client(service, **kwargs):
        endpoints.append(kwargs['endpoint_url'])
        return MagicMock()

    monkeypatch.setattr(storage_minio.boto3, 'client', create_client)
    monkeypatch.setattr(storage_minio, '_client', None)
    monkeypatch.setattr(storage_minio, '_client_config', None)
    monkeypatch.setenv('MINIO_ENDPOINT', 'http://minio-one:9000')
    monkeypatch.delenv('MINIO_PUBLIC_ENDPOINT', raising=False)
    storage_minio.get_minio_client()
    monkeypatch.setenv('MINIO_ENDPOINT', 'http://minio-two:9000')
    storage_minio.get_minio_client()

    assert endpoints == ['http://minio-one:9000', 'http://minio-two:9000']


def test_bucket_creation_only_follows_not_found(monkeypatch):
    s3 = MagicMock()
    monkeypatch.setattr(storage_minio.boto3, 'client', lambda *args, **kwargs: s3)
    client = storage_minio._MinioClient(
        ('http://minio:9000', 'access', 'secret', 'us-east-1', False, 'http://minio:9000')
    )
    s3.head_bucket.side_effect = _client_error('AccessDenied', 'HeadBucket')
    with pytest.raises(ClientError):
        client.bucket('private')
    s3.create_bucket.assert_not_called()

    s3.head_bucket.side_effect = _client_error('404', 'HeadBucket')
    client.bucket('missing')
    s3.create_bucket.assert_called_once_with(Bucket='missing')


def test_storage_public_urls_and_logo_delete_follow_selected_provider(monkeypatch):
    client = MagicMock()
    client.public_url.return_value = 'https://objects.example.test/apps/app-1.png'
    client.object_name_from_url.return_value = 'app-1.png'
    monkeypatch.setattr(storage, '_get_storage_client', lambda: client)
    monkeypatch.setenv('STORAGE_BACKEND', 'minio')
    monkeypatch.setattr(storage, 'omi_apps_bucket', 'apps')

    assert storage._public_object_url('apps', 'app-1.png') == 'https://objects.example.test/apps/app-1.png'
    assert storage.is_app_logo_url('https://objects.example.test/apps/app-1.png') is True
    storage.delete_app_logo('https://objects.example.test/apps/app-1.png')

    client.bucket.return_value.blob.assert_called_once_with('app-1.png')
    client.bucket.return_value.blob.return_value.delete.assert_called_once_with()


def test_signed_url_cache_is_namespaced_by_provider_and_bucket(monkeypatch):
    blob = MagicMock()
    blob.name = 'folder/a.json'
    blob.bucket_name = 'private'
    blob.bucket = None
    blob.generate_signed_url.return_value = 'https://objects.example.test/private/folder/a.json?signature=x'
    get_cached = MagicMock(return_value=None)
    cache = MagicMock()
    monkeypatch.setenv('STORAGE_BACKEND', 'minio')
    monkeypatch.setattr(storage, 'get_cached_signed_url', get_cached)
    monkeypatch.setattr(storage, 'cache_signed_url', cache)

    result = storage._get_signed_url(blob, 15)

    assert result.startswith('https://objects.example.test/')
    get_cached.assert_called_once_with('minio:private:folder/a.json')
    cache.assert_called_once_with(
        'minio:private:folder/a.json', 'https://objects.example.test/private/folder/a.json?signature=x', 900
    )


def test_parity_pack_export_uses_unified_storage_provider(monkeypatch):
    selected = MagicMock()
    monkeypatch.setattr(storage, 'get_storage_client', lambda: selected)

    assert parity_pack_export._storage_client() is selected
    assert parity_pack_export.resolve_export_target({'OMI_PARITY_PACK_STORAGE_URI': 's3://parity/private/v0'}) == (
        'parity',
        'private/v0',
    )


def test_account_object_reconciliation_covers_all_uid_namespaces_without_prefix_collision(monkeypatch):
    class Blob:
        def __init__(self, bucket, name):
            self.bucket = bucket
            self.name = name

        def delete(self):
            self.bucket.names.remove(self.name)

    class Bucket:
        def __init__(self, names):
            self.names = list(names)

        def list_blobs(self, prefix=None):
            return [Blob(self, name) for name in list(self.names) if name.startswith(prefix or '')]

    buckets = {
        'speech': Bucket(['u1/speech.wav', 'u10/speech.wav']),
        'postprocessing': Bucket(['syncing/u1/job/audio.wav', 'syncing/u10/job/audio.wav']),
        'recordings': Bucket(['u1/c1.wav']),
        'chat': Bucket(['u1/file.txt']),
        'private': Bucket(
            [
                'chunks/u1/c1/1.opus',
                'audio/u1/c1/a.wav',
                'merged/u1/c1/a.wav',
                'playback/u1/c1/a.mp3',
                'chunks/u10/c1/1.opus',
            ]
        ),
        'temporal': Bucket(
            [
                '_temp/u1/upload.wav',
                '_temp/u10/upload.wav',
                'syncing/u1/job/upload.wav',
                'syncing/u10/job/upload.wav',
            ]
        ),
    }
    client = MagicMock()
    client.bucket.side_effect = buckets.__getitem__
    monkeypatch.setattr(storage, '_get_storage_client', lambda: client)
    monkeypatch.setattr(storage, 'speech_profiles_bucket', 'speech')
    monkeypatch.setattr(storage, 'postprocessing_audio_bucket', 'postprocessing')
    monkeypatch.setattr(storage, 'memories_recordings_bucket', 'recordings')
    monkeypatch.setattr(storage, 'chat_files_bucket', 'chat')
    monkeypatch.setattr(storage, 'private_cloud_sync_bucket', 'private')
    monkeypatch.setattr(storage, 'syncing_local_bucket', 'temporal')

    assert storage.count_user_owned_objects('u1') == 10
    assert storage.delete_all_user_owned_objects('u1') == 10
    assert storage.count_user_owned_objects('u1') == 0
    assert buckets['speech'].names == ['u10/speech.wav']
    assert buckets['postprocessing'].names == ['syncing/u10/job/audio.wav']
    assert buckets['private'].names == ['chunks/u10/c1/1.opus']
    assert buckets['temporal'].names == ['_temp/u10/upload.wav', 'syncing/u10/job/upload.wav']
