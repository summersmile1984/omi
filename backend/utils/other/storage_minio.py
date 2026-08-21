"""MinIO object-storage shim for local dev — GCS-style API over S3/boto3.

Replaces ``google.cloud.storage`` for the self-hosted 4C8G deployment. Exposes
the same surface ``utils/other/storage.py`` uses (bucket().blob().upload_* /
download_* / delete / exists / list_blobs / generate_signed_url) so storage.py
needs only a client-construction switch (``STORAGE_BACKEND=minio``).

All code lives here; the only upstream touch is the branch in
``_get_storage_client()``.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Iterator, Optional

import boto3  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://127.0.0.1:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = os.getenv("MINIO_SECURE", "0") == "1"
MINIO_REGION = os.getenv("MINIO_REGION", "us-east-1")

_client: Any = None
_lock = threading.Lock()


class _MinioBlob:
    """GCS-style blob over a MinIO object."""

    def __init__(self, s3: Any, bucket: str, name: str) -> None:
        self._s3 = s3
        self._bucket = bucket
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def upload_from_string(self, data: str | bytes, content_type: Optional[str] = None) -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._name,
            Body=data.encode() if isinstance(data, str) else data,
            ContentType=content_type or "application/octet-stream",
        )

    def upload_from_filename(self, filename: str, content_type: Optional[str] = None) -> None:
        self._s3.upload_file(filename, self._bucket, self._name)

    def download_as_bytes(self) -> bytes:
        resp = self._s3.get_object(Bucket=self._bucket, Key=self._name)
        return resp["Body"].read()

    def download_to_filename(self, filename: str) -> None:
        self._s3.download_file(self._bucket, self._name, filename)

    def delete(self) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=self._name)

    def exists(self) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._name)
            return True
        except Exception:
            return False

    def generate_signed_url(self, expiration: int = 3600, method: str = "GET") -> str:
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": self._name},
            ExpiresIn=expiration,
        )


class _MinioBucket:
    """GCS-style bucket over MinIO."""

    def __init__(self, s3: Any, name: str) -> None:
        self._s3 = s3
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def blob(self, name: str) -> _MinioBlob:
        return _MinioBlob(self._s3, self._name, name)

    def list_blobs(self, prefix: Optional[str] = None) -> Iterator[_MinioBlob]:
        kwargs: Dict[str, Any] = {"Bucket": self._name}
        if prefix:
            kwargs["Prefix"] = prefix
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []):
                yield _MinioBlob(self._s3, self._name, obj["Key"])


class _MinioClient:
    """GCS-style client: ``bucket(name)`` over the MinIO S3 endpoint."""

    def __init__(self) -> None:
        self._s3 = boto3.client(
            "s3",
            endpoint_url=MINIO_ENDPOINT,
            aws_access_key_id=MINIO_ACCESS_KEY,
            aws_secret_access_key=MINIO_SECRET_KEY,
            region_name=MINIO_REGION,
            use_ssl=MINIO_SECURE,
        )

    def bucket(self, name: str) -> _MinioBucket:
        self._ensure_bucket(name)
        return _MinioBucket(self._s3, name)

    def _ensure_bucket(self, name: str) -> None:
        try:
            self._s3.head_bucket(Bucket=name)
        except Exception:
            try:
                self._s3.create_bucket(Bucket=name)
                logger.info("MinIO created bucket %s", name)
            except Exception as exc:  # pragma: no cover - race or permissions
                logger.warning("MinIO bucket %s create failed: %s", name, exc)


def get_minio_client() -> Any:
    """Return the process-wide MinIO client (GCS-style surface)."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = _MinioClient()
    return _client
