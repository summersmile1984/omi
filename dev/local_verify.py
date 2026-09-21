#!/usr/bin/env python3
"""End-to-end proof that the local data plane is actually working.

`dev/local.sh verify` runs this after `dev/local.sh up`. Every check is a real
round trip against a running service:

  1. postgres   firestore-pg schema migrated, collections registered
  2. redis      authenticated ping + write/read/delete round trip
  3. minio      S3 round trip (put, stat, get, delete) on a probe object
  4. auth       Better Auth signup, session-backed JWT issuance, JWKS served
  5. backend    required backend health
  6. product    authenticated action-item create/read/delete

The checkout runtime is started by dev/local.sh up; a missing backend is a
failed prerequisite, never a skipped success.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
# The backend runs with backend/ as its working directory, and the storage shim
# writes profile downloads to a relative `_temp/` path — verify must match that
# working directory or it would test a different environment than production.
os.chdir(BACKEND_DIR)
(BACKEND_DIR / "_temp").mkdir(exist_ok=True)


BACKEND_URL = os.environ.get("OMI_LOCAL_BACKEND_URL", "http://127.0.0.1:8100").rstrip("/")
AUTH_URL = os.environ.get("OMI_LOCAL_AUTH_URL", "http://127.0.0.1:3000").rstrip("/")
AUTH_DEV_ISSUER_SECRET = os.environ.get("AUTH_DEV_ISSUER_SECRET", "")
RUN_UID = f"local-verify-{int(time.time())}"


class CheckFailed(Exception):
    """A check proved the stack is not working."""


def http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict | None = None,
    timeout: float = 15.0,
) -> tuple[int, str]:
    data = None
    request_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["content-type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except urllib.error.URLError as error:
        if isinstance(error.reason, ConnectionError):
            return 0, "connection refused"
        raise


def _write_probe_wav(path: Path, seconds: float = 0.25, rate: int = 16000) -> int:
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", 0) for _ in range(frames)))
    return frames * 2


# --------------------------------------------------------------------- checks
def check_postgres() -> dict:
    import sqlalchemy as sa

    # Keep the psycopg3 driver suffix: the shim's DSN is `postgresql+psycopg://`,
    # and plain `postgresql://` would make SQLAlchemy look for psycopg2.
    dsn = os.environ.get("FIRESTORE_PG_DSN", "")
    if not dsn:
        raise CheckFailed("FIRESTORE_PG_DSN is not set for the verify process")
    engine = sa.create_engine(dsn)
    with engine.connect() as connection:
        migrations = connection.execute(sa.text("SELECT count(*) FROM firestore_pg_schema_migrations")).scalar_one()
        if not migrations:
            raise CheckFailed("firestore_pg_schema_migrations is empty — the shim never migrated")
        collections = (
            connection.execute(sa.text("SELECT collection_id FROM firestore_pg_collections ORDER BY collection_id"))
            .scalars()
            .all()
        )
        if not collections:
            raise CheckFailed("no collections registered — the shim did not materialize any table")
        tables = connection.execute(
            sa.text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name LIKE 'f\\_%'"
            )
        ).scalar_one()
    return {
        "detail": f"{migrations} migrations, {len(collections)} collections, {tables} collection tables",
        "evidence": {"migrations": migrations, "collections": list(collections)[:10], "collection_tables": tables},
    }


def check_redis() -> dict:
    import redis

    host = os.environ.get("REDIS_DB_HOST", "127.0.0.1")
    port = int(os.environ.get("REDIS_DB_PORT", "6379"))
    password = os.environ.get("REDIS_DB_PASSWORD") or None
    client = redis.Redis(host=host, port=port, password=password, socket_connect_timeout=5, socket_timeout=5)
    if not client.ping():
        raise CheckFailed(f"redis at {host}:{port} did not answer PING")
    key = f"omi-local-verify:{RUN_UID}"
    client.set(key, "ok", ex=60)
    value = client.get(key)
    client.delete(key)
    if value != b"ok":
        raise CheckFailed("redis write/read round trip returned the wrong value")
    return {"detail": f"ping + set/get/delete OK on {host}:{port}", "evidence": {"host": host, "port": port}}


def check_storage() -> dict:
    """Real S3 round trip against the local MinIO, without the app's shim.

    The app's storage helper needs the self-hosted command profile (it reports
    through the adapter the image installs), so this check talks to the same
    server directly and proves the credential, the bucket and the object path
    the runtime would use.
    """
    import boto3
    from botocore.config import Config

    endpoint = os.environ["MINIO_ENDPOINT"]
    bucket = os.environ.get("BUCKET_SPEECH_PROFILES", "omi-speech-profiles")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["MINIO_ACCESS_KEY"],
        aws_secret_access_key=os.environ["MINIO_SECRET_KEY"],
        region_name=os.environ.get("MINIO_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:
        client.create_bucket(Bucket=bucket)

    key = f"{RUN_UID}/speech_profile.wav"
    payload = b"omi-local-verify"
    client.put_object(Bucket=bucket, Key=key, Body=payload)
    head = client.head_object(Bucket=bucket, Key=key)
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    client.delete_object(Bucket=bucket, Key=key)
    if head["ContentLength"] != len(payload) or body != payload:
        raise CheckFailed("object round trip returned different bytes than were stored")
    return {
        "detail": f"put/head/get/delete of {len(payload)} bytes in {bucket} at {endpoint}",
        "evidence": {"bucket": bucket, "key": key, "endpoint": endpoint},
    }


def issue_principal(label: str) -> tuple[str, str, str]:
    """Sign a principal up and have the issuer mint a session-backed JWT.

    The self-hosted issuer refuses a uid with no user, so this creates a real Better
    Auth account (signup) and then asks /auth-issue for a token. Shared by the auth
    check and the product round trip: both need a principal the backend accepts, and
    each gets its own address because one signup per email is all Better Auth allows.
    """
    if not AUTH_DEV_ISSUER_SECRET:
        raise CheckFailed("AUTH_DEV_ISSUER_SECRET is not set for the verify process")

    email = f"{RUN_UID}-{label}@example.test"
    signup_status, signup_body = http(
        "POST",
        f"{AUTH_URL}/api/auth/sign-up/email",
        body={"email": email, "password": "local-dev-password-123", "name": "Local Verify"},
    )
    if signup_status != 200:
        raise CheckFailed(f"Better Auth signup returned {signup_status}: {signup_body[:300]}")
    uid = json.loads(signup_body).get("user", {}).get("id")
    if not uid:
        raise CheckFailed("Better Auth signup returned no user id")

    status, payload = http(
        "POST",
        f"{AUTH_URL}/auth-issue",
        headers={"authorization": f"Bearer {AUTH_DEV_ISSUER_SECRET}"},
        body={"uid": uid},
    )
    if status != 200:
        raise CheckFailed(f"auth-server /auth-issue returned {status}: {payload[:300]}")
    token = json.loads(payload).get("token")
    if not token or token.count(".") != 2:
        raise CheckFailed("auth-server /auth-issue returned no well-formed JWT")
    return uid, token, email


def check_auth() -> dict:
    """Sign a principal up, mint a JWT, and confirm the JWKS the backend verifies."""
    uid, token, email = issue_principal("auth")

    jwks_status, jwks_body = http("GET", f"{AUTH_URL}/api/auth/jwks")
    if jwks_status != 200 or not json.loads(jwks_body).get("keys"):
        raise CheckFailed(f"JWKS endpoint returned {jwks_status} without keys")

    return {
        "detail": "signup → session-backed JWT issued, JWKS served for verification",
        "evidence": {
            "uid": uid,
            "email": email,
            "token_prefix": token[:12],
            "jwks_keys": len(json.loads(jwks_body)["keys"]),
        },
    }


def check_backend() -> dict:
    """Require the backend endpoint; absence is not a successful qualification."""
    status, payload = http("GET", f"{BACKEND_URL}/v1/health")
    if status == 0:
        raise CheckFailed(f"no backend on {BACKEND_URL} — run dev/local.sh up")
    if status != 200:
        raise CheckFailed(f"GET /v1/health returned {status}")
    return {"detail": f"GET /v1/health → {status}", "evidence": {"body": payload[:200]}}


def check_product() -> dict:
    """One authenticated product round trip through the backend checkout.

    The image-based Server OS runtime cannot run on an arm64 host: `deploy/self-host/Dockerfile`
    derives from an amd64 upstream base, the compose file pins `platform: linux/amd64` for the
    model services, and the fixture passes `--platform=linux/amd64` to every build and run. The
    x86 runner covers that packaging. What an arm64 checkout can still exercise end to end is
    the product surface the image serves: a real JWT from the local issuer, a write through the
    fork entry point into PostgreSQL, and a read back of the same row.
    """
    status, _ = http("GET", f"{BACKEND_URL}/v1/health")
    if status == 0:
        raise CheckFailed(f"no backend on {BACKEND_URL} — run dev/local.sh up")
    uid, token, _ = issue_principal("product")
    headers = {"authorization": f"Bearer {token}"}
    description = f"local verify round trip {RUN_UID}"

    create_status, create_body = http(
        "POST", f"{BACKEND_URL}/v1/action-items", headers=headers, body={"description": description}
    )
    if create_status != 200:
        raise CheckFailed(f"POST /v1/action-items returned {create_status}: {create_body[:300]}")
    item_id = json.loads(create_body).get("id")
    if not item_id:
        raise CheckFailed("POST /v1/action-items returned no id")

    read_status, read_body = http("GET", f"{BACKEND_URL}/v1/action-items/{item_id}", headers=headers)
    if read_status != 200 or json.loads(read_body).get("description") != description:
        raise CheckFailed(f"GET /v1/action-items/{item_id} returned {read_status}: {read_body[:300]}")

    # Leave the local database as it was found; a failed cleanup is not a product failure.
    http("DELETE", f"{BACKEND_URL}/v1/action-items/{item_id}", headers=headers)

    return {
        "detail": "JWT → POST /v1/action-items → GET the same row → DELETE (PostgreSQL)",
        "evidence": {"uid": uid, "action_item_id": item_id, "description": description},
    }


CHECKS = (
    ("postgres", "firestore_pg schema over PostgreSQL", check_postgres),
    ("redis", "authenticated queue/cache backend", check_redis),
    ("storage", "S3 round trip against MinIO", check_storage),
    ("auth", "Better Auth signup → JWT → JWKS", check_auth),
    ("backend", "backend health, when a backend is running", check_backend),
    ("product", "authenticated product round trip, when a backend is running", check_product),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", help="path for the JSON evidence file")
    parser.add_argument("--quiet", action="store_true", help="only print the summary line")
    arguments = parser.parse_args()

    started = datetime.now(timezone.utc)
    results = []
    for name, description, function in CHECKS:
        began = time.monotonic()
        try:
            outcome = function()
            status = "skip" if outcome.get("skipped") else "pass"
            results.append(
                {
                    "check": name,
                    "description": description,
                    "status": status,
                    "detail": outcome.get("detail", ""),
                    "evidence": outcome.get("evidence", {}),
                    "seconds": round(time.monotonic() - began, 3),
                }
            )
            if not arguments.quiet:
                label = "SKIP" if status == "skip" else "PASS"
                print(f"  {label}  {name:<9} {outcome.get('detail', '')}")
        except Exception as error:  # noqa: BLE001 - a failed check must not abort the run
            detail = f"{type(error).__name__}: {error}" if not isinstance(error, CheckFailed) else str(error)
            results.append(
                {
                    "check": name,
                    "description": description,
                    "status": "fail",
                    "detail": detail,
                    "evidence": {},
                    "seconds": round(time.monotonic() - began, 3),
                }
            )
            if not arguments.quiet:
                print(f"  FAIL  {name:<9} {detail}")

    failed = [result for result in results if result["status"] == "fail"]
    skipped = [result for result in results if result["status"] == "skip"]
    evidence = {
        "kind": "omi-local-dev-verify",
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "backend_url": BACKEND_URL,
        "auth_url": AUTH_URL,
        "uid": RUN_UID,
        "ok": not failed,
        "passed": len(results) - len(failed) - len(skipped),
        "skipped": len(skipped),
        "checks": results,
    }
    if arguments.evidence:
        destination = Path(arguments.evidence)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

    print("")
    if failed:
        print(f"local dev verify: FAILED ({len(failed)}/{len(results)} checks)")
        return 1
    passed = len(results) - len(skipped)
    suffix = f", {len(skipped)} skipped" if skipped else ""
    print(f"local dev verify: PASSED ({passed}/{passed} checks{suffix})")
    if arguments.evidence:
        print(f"evidence: {arguments.evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
