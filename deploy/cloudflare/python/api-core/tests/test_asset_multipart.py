import asyncio
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import entry  # noqa: E402


def signed_context(secret: str, uid: str) -> tuple[str, str]:
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps({"uid": uid, "authority": "better-auth", "requestId": "test"}, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return encoded, base64.urlsafe_b64encode(signature).decode().rstrip("=")


class MultipartDb:
    def __init__(self):
        self.uploads = {}
        self.parts = {}
        self.assets = {}
        self.cleanup = {}

    def prepare(self, sql):
        return MultipartStatement(self, sql)

    async def batch(self, statements):
        snapshot = (dict(self.uploads), dict(self.parts), dict(self.assets), dict(self.cleanup))
        try:
            for statement in statements:
                await statement.run()
        except Exception:
            self.uploads, self.parts, self.assets, self.cleanup = snapshot
            raise


class MultipartStatement:
    def __init__(self, db, sql):
        self.db = db
        self.sql = " ".join(sql.split())
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def first(self):
        if "FROM cf_asset_multipart_uploads" in self.sql:
            if "idempotency_key = ?" in self.sql:
                uid, idempotency_key, _now = self.args
                rows = [
                    row
                    for row in self.db.uploads.values()
                    if row["uid"] == uid and row["idempotency_key"] == idempotency_key and row["state"] == "pending"
                ]
            elif "object_key = ?" in self.sql and "upload_id = ?" not in self.sql:
                uid, object_key, _now = self.args
                rows = [
                    row
                    for row in self.db.uploads.values()
                    if row["uid"] == uid and row["object_key"] == object_key and row["state"] == "pending"
                ]
            elif "upload_id = ?" in self.sql:
                uid, upload_id = self.args[:2]
                rows = [row for row in self.db.uploads.values() if row["uid"] == uid and row["upload_id"] == upload_id]
            else:
                raise AssertionError(self.sql)
            if not rows:
                return None
            row = dict(rows[0])
            if "storage_key" not in self.sql:
                row.pop("storage_key", None)
            return row
        if "FROM cf_asset_objects" in self.sql:
            uid, object_key = self.args
            row = self.db.assets.get((uid, object_key))
            return dict(row) if row else None
        raise AssertionError(self.sql)

    async def all(self):
        if "FROM cf_asset_multipart_parts" not in self.sql:
            raise AssertionError(self.sql)
        uid, upload_id = self.args
        rows = [
            dict(row)
            for (part_uid, part_upload_id, _part_number), row in self.db.parts.items()
            if part_uid == uid and part_upload_id == upload_id
        ]
        rows.sort(key=lambda row: row["part_number"])
        return {"results": rows}

    async def run(self):
        if self.sql.startswith("DELETE FROM cf_asset_multipart_uploads WHERE uid = ? AND expires_at <= ?"):
            uid, now = self.args
            for key, row in list(self.db.uploads.items()):
                if row["uid"] == uid and row["expires_at"] <= now:
                    self.db.uploads.pop(key, None)
                    for part_key in list(self.db.parts):
                        if part_key[:2] == key:
                            self.db.parts.pop(part_key, None)
            return
        if self.sql.startswith("INSERT INTO cf_asset_multipart_uploads"):
            (
                uid,
                upload_id,
                object_key,
                storage_key,
                content_type,
                idempotency_key,
                expected_size,
                expected_checksum,
                expires_at,
                created_at,
                updated_at,
            ) = self.args
            if (uid, idempotency_key) in {(row["uid"], row["idempotency_key"]) for row in self.db.uploads.values()}:
                raise RuntimeError("unique idempotency conflict")
            if (uid, object_key, "pending") in {
                (row["uid"], row["object_key"], row["state"]) for row in self.db.uploads.values()
            }:
                raise RuntimeError("unique object conflict")
            self.db.uploads[(uid, upload_id)] = {
                "uid": uid,
                "upload_id": upload_id,
                "object_key": object_key,
                "storage_key": storage_key,
                "content_type": content_type,
                "idempotency_key": idempotency_key,
                "expected_size": expected_size,
                "expected_checksum_sha256": expected_checksum,
                "state": "pending",
                "expires_at": expires_at,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("INSERT INTO cf_asset_multipart_parts"):
            uid, upload_id, part_number, size, etag, checksum, created_at, updated_at = self.args
            self.db.parts[(uid, upload_id, part_number)] = {
                "uid": uid,
                "upload_id": upload_id,
                "part_number": part_number,
                "size": size,
                "etag": etag,
                "checksum_sha256": checksum,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("INSERT OR IGNORE INTO cf_asset_cleanup_tasks"):
            if "SELECT storage_key" in self.sql:
                return
            storage_key, uid, logical_key, content_type, not_before, created_at, updated_at = self.args
            self.db.cleanup[storage_key] = {
                "storage_key": storage_key,
                "uid": uid,
                "logical_key": logical_key,
                "content_type": content_type,
                "not_before": not_before,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("INSERT INTO cf_asset_objects"):
            uid, object_key, storage_key, content_type, size, etag, checksum, created_at, updated_at = self.args
            self.db.assets[(uid, object_key)] = {
                "uid": uid,
                "object_key": object_key,
                "storage_key": storage_key,
                "content_type": content_type,
                "size": size,
                "etag": etag,
                "checksum_sha256": checksum,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            return
        if self.sql.startswith("DELETE FROM cf_asset_multipart_parts"):
            uid, upload_id = self.args
            for part_key in list(self.db.parts):
                if part_key[:2] == (uid, upload_id):
                    self.db.parts.pop(part_key, None)
            return
        if self.sql.startswith("DELETE FROM cf_asset_multipart_uploads"):
            uid, upload_id = self.args
            self.db.uploads.pop((uid, upload_id), None)
            return
        if self.sql.startswith("SELECT storage_key"):
            return
        raise AssertionError(self.sql)


class MultipartObject:
    def __init__(self, content: bytes):
        self.content = content

    async def arrayBuffer(self):
        return self.content


class MultipartHandle:
    def __init__(self, bucket, key, upload_id):
        self.bucket = bucket
        self.key = key
        self.upload_id = upload_id

    async def uploadPart(self, part_number, body):
        content = bytes(body)
        self.bucket.uploads[self.upload_id]["parts"][part_number] = content
        return SimpleNamespace(etag=f'"part-{part_number}-{len(content)}"', partNumber=part_number)

    async def complete(self, parts):
        upload = self.bucket.uploads[self.upload_id]
        content = b"".join(upload["parts"][int(part["partNumber"])] for part in parts)
        self.bucket.objects[self.key] = content
        upload["completed"] = True
        return SimpleNamespace(httpEtag='"asset-etag"', key=self.key)

    async def abort(self):
        self.bucket.uploads.pop(self.upload_id, None)


class MultipartBucket:
    def __init__(self):
        self.uploads = {}
        self.objects = {}
        self.next_id = 1

    async def createMultipartUpload(self, key, _options=None):
        upload_id = f"upload-{self.next_id}"
        self.next_id += 1
        self.uploads[upload_id] = {"key": key, "parts": {}, "completed": False}
        return SimpleNamespace(key=key, uploadId=upload_id)

    def resumeMultipartUpload(self, key, upload_id):
        upload = self.uploads.get(upload_id)
        if not upload or upload["key"] != key:
            raise RuntimeError("unknown upload")
        return MultipartHandle(self, key, upload_id)

    async def get(self, key, _options=None):
        content = self.objects.get(key)
        return MultipartObject(content) if content is not None else None

    async def delete(self, key):
        self.objects.pop(key, None)


class Request:
    def __init__(self, env, headers, *, query_params=None, body=b""):
        self.scope = {"env": env}
        self.headers = headers
        self.query_params = query_params or {}
        self._body = body

    async def body(self):
        return self._body

    async def stream(self):
        yield self._body


def make_env(uid="user-1"):
    del uid
    secret = "asset-secret"
    encoded, signature = signed_context(secret, "user-1")
    db = MultipartDb()
    bucket = MultipartBucket()
    env = SimpleNamespace(
        APP_DB=db,
        ASSETS=bucket,
        INTERNAL_ASSERTION_SECRET=secret,
    )
    headers = {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": signature,
        "content-type": "application/octet-stream",
    }
    return env, headers, db, bucket


def test_multipart_asset_upload_is_resumable_and_commits_canonical_metadata(monkeypatch):
    monkeypatch.setattr(entry, "MIN_ASSET_MULTIPART_PART_BYTES", 1)
    env, headers, db, bucket = make_env()
    first = b"hello "
    second = b"cloudflare"
    full = first + second
    create = asyncio.run(
        entry.post_asset(
            "uploads/greeting.txt",
            Request(
                env,
                {
                    **headers,
                    "idempotency-key": "greeting-1",
                    "x-asset-size": str(len(full)),
                    "x-content-sha256": hashlib.sha256(full).hexdigest(),
                },
                query_params={"action": "mpu-create"},
            ),
        )
    )
    assert create.status_code == 201
    upload_id = json.loads(create.body)["upload_id"]

    part_one = asyncio.run(
        entry.put_asset(
            "uploads/greeting.txt",
            Request(
                env,
                headers,
                query_params={"action": "mpu-uploadpart", "uploadId": upload_id, "partNumber": "1"},
                body=first,
            ),
        )
    )
    part_two = asyncio.run(
        entry.put_asset(
            "uploads/greeting.txt",
            Request(
                env,
                headers,
                query_params={"action": "mpu-uploadpart", "uploadId": upload_id, "partNumber": "2"},
                body=second,
            ),
        )
    )
    assert part_one["part_number"] == 1
    assert part_two["part_number"] == 2

    completed = asyncio.run(
        entry.post_asset(
            "uploads/greeting.txt",
            Request(
                env,
                headers,
                query_params={"action": "mpu-complete", "uploadId": upload_id},
                body=json.dumps(
                    {"parts": [part_one, part_two], "checksum_sha256": hashlib.sha256(full).hexdigest()}
                ).encode(),
            ),
        )
    )
    assert completed == {
        "status": "ok",
        "key": "uploads/greeting.txt",
        "size": len(full),
        "etag": '"asset-etag"',
        "checksum_sha256": hashlib.sha256(full).hexdigest(),
    }
    assert db.uploads == {}
    assert db.parts == {}
    assert db.assets[("user-1", "user-1/uploads/greeting.txt")]["size"] == len(full)
    assert len(bucket.objects) == 1

    retried = asyncio.run(
        entry.post_asset(
            "uploads/greeting.txt",
            Request(
                env,
                headers,
                query_params={"action": "mpu-complete", "uploadId": upload_id},
                body=json.dumps({"parts": [part_one, part_two]}).encode(),
            ),
        )
    )
    assert retried == completed


def test_multipart_asset_upload_is_uid_scoped_and_abort_is_idempotent():
    env, headers, _db, _bucket = make_env()
    create = asyncio.run(
        entry.post_asset(
            "uploads/abort.bin",
            Request(env, {**headers, "idempotency-key": "abort-1"}, query_params={"action": "mpu-create"}),
        )
    )
    upload_id = json.loads(create.body)["upload_id"]
    other_encoded, other_signature = signed_context("asset-secret", "user-2")
    other = Request(
        env,
        {**headers, "x-omi-auth-context": other_encoded, "x-omi-internal-signature": other_signature},
        query_params={"action": "mpu-uploadpart", "uploadId": upload_id, "partNumber": "1"},
        body=b"nope",
    )
    assert asyncio.run(entry.put_asset("uploads/abort.bin", other)).status_code == 404

    aborted = asyncio.run(
        entry.delete_asset(
            "uploads/abort.bin",
            Request(env, headers, query_params={"action": "mpu-abort", "uploadId": upload_id}),
        )
    )
    assert aborted.status_code == 204
    repeated = asyncio.run(
        entry.delete_asset(
            "uploads/abort.bin",
            Request(env, headers, query_params={"action": "mpu-abort", "uploadId": upload_id}),
        )
    )
    assert repeated.status_code == 204


def test_multipart_asset_completion_rejects_part_ledger_mismatch():
    env, headers, _db, _bucket = make_env()
    create = asyncio.run(
        entry.post_asset(
            "uploads/reject.bin",
            Request(env, {**headers, "idempotency-key": "reject-1"}, query_params={"action": "mpu-create"}),
        )
    )
    upload_id = json.loads(create.body)["upload_id"]
    part = asyncio.run(
        entry.put_asset(
            "uploads/reject.bin",
            Request(
                env,
                headers,
                query_params={"action": "mpu-uploadpart", "uploadId": upload_id, "partNumber": "1"},
                body=b"part",
            ),
        )
    )
    part["etag"] = '"tampered"'
    response = asyncio.run(
        entry.post_asset(
            "uploads/reject.bin",
            Request(
                env,
                headers,
                query_params={"action": "mpu-complete", "uploadId": upload_id},
                body=json.dumps({"parts": [part]}).encode(),
            ),
        )
    )
    assert response.status_code == 422
