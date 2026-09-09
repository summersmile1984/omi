"""Explicit MinIO client for the self-host object-storage boundary.

Transport failures are errors, never object absence. Public signing uses the
operator's public origin while API transfers use the internal service origin.
"""

from dataclasses import dataclass, field
import os
import threading
from urllib.parse import urlsplit

import boto3
from botocore.config import Config as S3Config
from botocore.exceptions import ClientError

from .storage_minio_blob import MinioBlob

_client = None
_client_config = None
_lock = threading.Lock()


def missing(error):
    return isinstance(error, ClientError) and str(error.response.get('Error', {}).get('Code')) in {
        '404',
        'NoSuchKey',
        'NoSuchBucket',
        'NotFound',
    }


def origin(value, name):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ('http', 'https')
        or not parsed.hostname
        or parsed.username
        or parsed.path not in ('', '/')
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f'{name} must be an explicit HTTP(S) service origin')
    return value.rstrip('/')


@dataclass(frozen=True)
class Config:
    endpoint: str
    public_endpoint: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    region: str = 'us-east-1'

    @classmethod
    def from_env(cls):
        endpoint = origin(os.environ.get('MINIO_ENDPOINT', ''), 'MINIO_ENDPOINT')
        public = origin(os.environ.get('MINIO_PUBLIC_ENDPOINT', ''), 'MINIO_PUBLIC_ENDPOINT')
        access, secret = os.environ.get('MINIO_ACCESS_KEY', ''), os.environ.get('MINIO_SECRET_KEY', '')
        if not access or not secret:
            raise ValueError('MINIO_ACCESS_KEY and MINIO_SECRET_KEY are required')
        return cls(endpoint, public, access, secret, os.environ.get('MINIO_REGION', 'us-east-1'))


class _MinioBucket:
    def __init__(self, client, name):
        self._client, self.name = client, name

    def blob(self, name):
        if not isinstance(name, str) or not name:
            raise ValueError('object name must be a nonempty string')
        return MinioBlob(self, name)

    def list_blobs(self, prefix=None):
        kwargs = {'Bucket': self.name, 'Prefix': prefix or ''}
        for page in self._client._s3.get_paginator('list_objects_v2').paginate(**kwargs):
            for obj in page.get('Contents', []):
                blob = self.blob(obj['Key'])
                blob.size = obj['Size']
                yield blob


class _MinioClient:
    def __init__(self, config):
        self.config = config
        options = dict(
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
            config=S3Config(
                signature_version='s3v4',
                s3={'addressing_style': 'path'},
                connect_timeout=5,
                read_timeout=30,
                retries={'max_attempts': 2},
            ),
        )
        self._s3 = boto3.client('s3', endpoint_url=config.endpoint, **options)
        self._signer = boto3.client('s3', endpoint_url=config.public_endpoint, **options)

    def bucket(self, name):
        if not isinstance(name, str) or not name:
            raise ValueError('self-host storage bucket must be configured')
        try:
            self._s3.head_bucket(Bucket=name)
        except ClientError as error:
            if not missing(error):
                raise
            try:
                self._s3.create_bucket(Bucket=name)
            except ClientError as race:
                if race.response.get('Error', {}).get('Code') != 'BucketAlreadyOwnedByYou':
                    raise
        return _MinioBucket(self, name)


def get_minio_client():
    global _client, _client_config
    config = Config.from_env()
    with _lock:
        if _client is None or _client_config != config:
            _client = _MinioClient(config)
            _client_config = config
        return _client
