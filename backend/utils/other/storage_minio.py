"""GCS-compatible object-storage surface backed by MinIO/S3."""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, NoReturn, Optional
from urllib.parse import quote, unquote, urlparse

import boto3  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

_client: Any = None
_client_config: Optional[tuple[str, str, str, str, bool, str]] = None
_lock = threading.Lock()
_policy_lock = threading.Lock()


def _error_code(error: BaseException) -> str:
    response_value = getattr(error, "response", {})
    response = response_value if isinstance(response_value, dict) else {}
    return str(response.get("Error", {}).get("Code", ""))


def _is_not_found(error: BaseException) -> bool:
    return _error_code(error) in {"404", "NoSuchBucket", "NoSuchBucketPolicy", "NoSuchKey", "NotFound"}


def _raise_not_found(error: BaseException, resource: str) -> NoReturn:
    if _is_not_found(error):
        raise NotFound(f"Object storage resource not found: {resource}") from error
    raise error


class _MinioBlob:
    """Subset of ``google.cloud.storage.Blob`` used by the backend."""

    def __init__(
        self,
        s3: Any,
        public_s3: Any,
        public_endpoint: str,
        bucket: str,
        name: str,
        *,
        size: Optional[int] = None,
    ) -> None:
        self._s3 = s3
        self._public_s3 = public_s3
        self._public_endpoint = public_endpoint.rstrip("/")
        self._bucket = bucket
        self._name = name
        self._size = size
        self._metadata: Optional[Dict[str, str]] = None
        self.cache_control: Optional[str] = None
        self.content_type: Optional[str] = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def bucket_name(self) -> str:
        return self._bucket

    @property
    def size(self) -> Optional[int]:
        if self._size is None:
            self.reload()
        return self._size

    @property
    def metadata(self) -> Optional[Dict[str, str]]:
        return self._metadata

    @metadata.setter
    def metadata(self, value: Optional[Mapping[str, Any]]) -> None:
        self._metadata = None if value is None else {str(key): str(item) for key, item in value.items()}

    @property
    def public_url(self) -> str:
        bucket = quote(self._bucket, safe="")
        object_name = quote(self._name, safe="/")
        return f"{self._public_endpoint}/{bucket}/{object_name}"

    def _write_args(self, content_type: Optional[str]) -> Dict[str, Any]:
        args: Dict[str, Any] = {}
        selected_content_type = content_type or self.content_type
        if selected_content_type:
            args["ContentType"] = selected_content_type
        if self.cache_control:
            args["CacheControl"] = self.cache_control
        if self._metadata is not None:
            args["Metadata"] = self._metadata
        return args

    def upload_from_string(self, data: str | bytes, content_type: Optional[str] = None) -> None:
        body = data.encode() if isinstance(data, str) else data
        self._s3.put_object(Bucket=self._bucket, Key=self._name, Body=body, **self._write_args(content_type))
        self._size = len(body)
        self.content_type = content_type or self.content_type

    def upload_from_filename(self, filename: str, content_type: Optional[str] = None) -> None:
        extra_args = self._write_args(content_type)
        kwargs: Dict[str, Any] = {"ExtraArgs": extra_args} if extra_args else {}
        self._s3.upload_file(filename, self._bucket, self._name, **kwargs)
        self._size = Path(filename).stat().st_size
        self.content_type = content_type or self.content_type

    def download_as_bytes(self) -> bytes:
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=self._name)
        except Exception as error:
            _raise_not_found(error, f"{self._bucket}/{self._name}")
        return response["Body"].read()

    def download_to_filename(self, filename: str) -> None:
        try:
            self._s3.download_file(self._bucket, self._name, filename)
        except Exception as error:
            _raise_not_found(error, f"{self._bucket}/{self._name}")

    def delete(self) -> None:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._name)
            self._s3.delete_object(Bucket=self._bucket, Key=self._name)
        except Exception as error:
            _raise_not_found(error, f"{self._bucket}/{self._name}")

    def exists(self) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._name)
            return True
        except Exception as error:
            if _is_not_found(error):
                return False
            raise

    def reload(self) -> None:
        try:
            response = self._s3.head_object(Bucket=self._bucket, Key=self._name)
        except Exception as error:
            _raise_not_found(error, f"{self._bucket}/{self._name}")
        self._size = int(response.get("ContentLength", 0))
        self._metadata = {str(key): str(value) for key, value in response.get("Metadata", {}).items()}
        self.cache_control = response.get("CacheControl")
        self.content_type = response.get("ContentType")

    def generate_signed_url(
        self,
        version: Optional[str] = None,
        expiration: int | float | datetime.timedelta = 3600,
        method: str = "GET",
    ) -> str:
        del version
        expires_in = int(expiration.total_seconds()) if isinstance(expiration, datetime.timedelta) else int(expiration)
        normalized_method = method.upper()
        operations = {"GET": "get_object", "PUT": "put_object", "DELETE": "delete_object"}
        if normalized_method not in operations:
            raise ValueError(f"unsupported signed URL method: {normalized_method}")
        return self._public_s3.generate_presigned_url(
            operations[normalized_method],
            Params={"Bucket": self._bucket, "Key": self._name},
            ExpiresIn=expires_in,
            HttpMethod=normalized_method,
        )

    def make_public(self) -> None:
        try:
            self._s3.put_object_acl(Bucket=self._bucket, Key=self._name, ACL="public-read")
            return
        except ClientError as error:
            if _error_code(error) not in {"AccessControlListNotSupported", "InvalidRequest", "NotImplemented"}:
                raise
        resource = f"arn:aws:s3:::{self._bucket}/{self._name}"
        with _policy_lock:
            try:
                response = self._s3.get_bucket_policy(Bucket=self._bucket)
                policy: Dict[str, Any] = json.loads(response["Policy"])
            except ClientError as error:
                if not _is_not_found(error):
                    raise
                policy = {"Version": "2012-10-17", "Statement": []}
            statement_value = policy.get("Statement", [])
            if isinstance(statement_value, dict):
                statements: list[Dict[str, Any]] = [statement_value]
            elif isinstance(statement_value, list) and all(isinstance(item, dict) for item in statement_value):
                statements = statement_value
            else:
                raise ValueError("MinIO bucket policy has an invalid Statement field")
            policy["Statement"] = statements
            public_statement = next(
                (statement for statement in statements if statement.get("Sid") == "OmiPublicObjectRead"), None
            )
            if public_statement is None:
                public_statement = {
                    "Sid": "OmiPublicObjectRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [],
                }
                statements.append(public_statement)
            resources = public_statement.get("Resource", [])
            if isinstance(resources, str):
                resources = [resources]
            if resource not in resources:
                resources.append(resource)
            public_statement["Resource"] = resources
            self._s3.put_bucket_policy(Bucket=self._bucket, Policy=json.dumps(policy, separators=(",", ":")))


class _MinioBucket:
    """Subset of ``google.cloud.storage.Bucket`` used by the backend."""

    def __init__(self, s3: Any, public_s3: Any, public_endpoint: str, name: str) -> None:
        self._s3 = s3
        self._public_s3 = public_s3
        self._public_endpoint = public_endpoint
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def blob(self, name: str) -> _MinioBlob:
        return _MinioBlob(self._s3, self._public_s3, self._public_endpoint, self._name, name)

    def list_blobs(self, prefix: Optional[str] = None) -> Iterator[_MinioBlob]:
        kwargs: Dict[str, Any] = {"Bucket": self._name}
        if prefix:
            kwargs["Prefix"] = prefix
        paginator = self._s3.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(**kwargs):
                for item in page.get("Contents", []):
                    yield _MinioBlob(
                        self._s3,
                        self._public_s3,
                        self._public_endpoint,
                        self._name,
                        item["Key"],
                        size=int(item.get("Size", 0)),
                    )
        except Exception as error:
            _raise_not_found(error, self._name)

    def copy_blob(self, source_blob: _MinioBlob, destination_bucket: "_MinioBucket", new_name: str) -> _MinioBlob:
        self._s3.copy_object(
            CopySource={"Bucket": source_blob.bucket_name, "Key": source_blob.name},
            Bucket=destination_bucket.name,
            Key=new_name,
        )
        return destination_bucket.blob(new_name)


class _MinioClient:
    """Subset of ``google.cloud.storage.Client`` backed by MinIO."""

    def __init__(self, config: tuple[str, str, str, str, bool, str]) -> None:
        endpoint, access_key, secret_key, region, secure, public_endpoint = config
        client_kwargs = {
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "region_name": region,
            "use_ssl": secure,
            # Do not inherit botocore's legacy SigV2 presign default. SigV2
            # incorporates an unsigned caller Content-Type into PUT validation,
            # which breaks otherwise valid signed URLs behind the public edge.
            "config": Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        }
        self._s3 = boto3.client("s3", endpoint_url=endpoint, **client_kwargs)
        self._public_s3 = (
            self._s3
            if public_endpoint.rstrip("/") == endpoint.rstrip("/")
            else boto3.client("s3", endpoint_url=public_endpoint, **client_kwargs)
        )
        self._public_endpoint = public_endpoint.rstrip("/")

    def bucket(self, name: str) -> _MinioBucket:
        if not name:
            raise ValueError("bucket name must be non-empty")
        self._ensure_bucket(name)
        return _MinioBucket(self._s3, self._public_s3, self._public_endpoint, name)

    def public_url(self, bucket: str, object_name: str) -> str:
        return _MinioBlob(self._s3, self._public_s3, self._public_endpoint, bucket, object_name).public_url

    def object_name_from_url(self, bucket: str, url: str) -> Optional[str]:
        expected = urlparse(self._public_endpoint)
        parsed = urlparse(url)
        if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
            return None
        endpoint_path = expected.path.rstrip("/")
        path = parsed.path
        if endpoint_path:
            if not path.startswith(f"{endpoint_path}/"):
                return None
            path = path[len(endpoint_path) :]
        prefix = f"/{quote(bucket, safe='')}/"
        if not path.startswith(prefix):
            return None
        return unquote(path[len(prefix) :])

    def _ensure_bucket(self, name: str) -> None:
        try:
            self._s3.head_bucket(Bucket=name)
            return
        except Exception as error:
            if not _is_not_found(error):
                raise
        try:
            self._s3.create_bucket(Bucket=name)
            logger.info("MinIO created bucket %s", name)
        except ClientError as error:
            if _error_code(error) not in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
                raise


def _get_minio_config() -> tuple[str, str, str, str, bool, str]:
    endpoint = os.getenv("MINIO_ENDPOINT", "http://127.0.0.1:9000").rstrip("/")
    return (
        endpoint,
        os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
        os.getenv("MINIO_SECRET_KEY", "minioadmin"),
        os.getenv("MINIO_REGION", "us-east-1"),
        os.getenv("MINIO_SECURE", "0").strip().lower() in {"1", "true", "yes"},
        os.getenv("MINIO_PUBLIC_ENDPOINT", endpoint).rstrip("/"),
    )


def get_minio_client() -> Any:
    """Return a cached MinIO client for the current runtime configuration."""
    global _client, _client_config
    config = _get_minio_config()
    if _client is None or _client_config != config:
        with _lock:
            if _client is None or _client_config != config:
                _client = _MinioClient(config)
                _client_config = config
    return _client
