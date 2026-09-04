"""Object methods consumed by the actual upstream audio/cache/file paths."""

from contextlib import contextmanager
from datetime import timedelta
import math
import tempfile
from urllib.parse import quote

from botocore.exceptions import ClientError
from google.cloud.exceptions import NotFound


class MinioBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name
        self.size = None
        self.metadata = None
        self.cache_control = None
        self.content_type = None

    @property
    def _client(self):
        return self.bucket._client

    @property
    def _selector(self):
        return {'Bucket': self.bucket.name, 'Key': self.name}

    @property
    def public_url(self):
        # This is an unsigned asset address, not a grant. Public catalogue
        # buckets require explicit operator policy; private audio uses signing.
        return f'{self._client.config.public_endpoint}/{quote(self.bucket.name)}/{quote(self.name, safe="/")}'

    def _extra(self, content_type=None):
        result = {'ContentType': content_type or self.content_type or 'application/octet-stream'}
        if self.metadata is not None:
            result['Metadata'] = {str(key): str(value) for key, value in self.metadata.items()}
        if self.cache_control is not None:
            result['CacheControl'] = self.cache_control
        return result

    def upload_from_string(self, data, content_type=None):
        data = data.encode() if isinstance(data, str) else data
        self._client._s3.put_object(**self._selector, Body=data, **self._extra(content_type))
        self.size = len(data)

    def upload_from_filename(self, filename, content_type=None):
        with open(filename, 'rb') as source:
            self._client._s3.upload_fileobj(source, self.bucket.name, self.name, ExtraArgs=self._extra(content_type))

    @contextmanager
    def open(self, mode, content_type=None):
        if mode != 'wb':
            raise ValueError('self-host streaming object writes require wb')
        # Spill after 8 MiB; do not buffer arbitrarily long recordings in RAM.
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as stream:
            yield stream
            stream.seek(0)
            self._client._s3.upload_fileobj(stream, self.bucket.name, self.name, ExtraArgs=self._extra(content_type))

    def reload(self):
        from .storage_minio import missing

        try:
            response = self._client._s3.head_object(**self._selector)
        except ClientError as error:
            if missing(error):
                raise NotFound('object does not exist') from error
            raise
        self.size = response['ContentLength']
        self.metadata = response.get('Metadata', {})
        self.cache_control = response.get('CacheControl')
        self.content_type = response.get('ContentType')

    def exists(self):
        try:
            self.reload()
            return True
        except NotFound:
            return False

    def download_as_bytes(self):
        from .storage_minio import missing

        try:
            response = self._client._s3.get_object(**self._selector)
        except ClientError as error:
            if missing(error):
                raise NotFound('object does not exist') from error
            raise
        try:
            return response['Body'].read()
        finally:
            response['Body'].close()

    def download_to_filename(self, filename):
        self._client._s3.download_file(self.bucket.name, self.name, filename)

    def delete(self):
        self._client._s3.delete_object(**self._selector)

    def generate_signed_url(self, expiration=3600, method='GET', version='v4'):
        if version != 'v4' or method != 'GET':
            raise ValueError('self-host private object links support only v4 GET signing')
        seconds = expiration.total_seconds() if isinstance(expiration, timedelta) else expiration
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 1 <= seconds <= 604800:
            raise ValueError('signed object lifetime must be 1..604800 seconds')
        return self._client._signer.generate_presigned_url(
            'get_object', Params=self._selector, ExpiresIn=int(seconds), HttpMethod='GET'
        )

    def make_public(self):
        raise ValueError('MinIO public asset policy must be provisioned explicitly by the operator')
