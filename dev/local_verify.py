#!/usr/bin/env python3
"""End-to-end proof that the Omi local dev stack is actually working.

`dev/local.sh verify` runs this after the stack is up. Every check exercises the
production code path (the same shim modules the backend imports at runtime) or a
real network round trip — nothing here asserts on configuration alone:

  1. postgres   firestore-pg schema migrated, collections registered
  2. redis      ping + write/read/delete round trip
  3. minio      backend storage shim: upload profile audio, read it back
  4. auth       Better Auth issues a JWT, the backend rejects it when absent
                and accepts it when present (the auth boundary, both directions)
  5. backend    backend health, plus an authenticated API round trip

Exit code is 0 only when every check passes. A JSON evidence file is written for
the run so the result can be attached to a PR or compared across days.
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
        migrations = connection.execute(
            sa.text("SELECT count(*) FROM firestore_pg_schema_migrations")
        ).scalar_one()
        if not migrations:
            raise CheckFailed("firestore_pg_schema_migrations is empty — the shim never migrated")
        collections = connection.execute(
            sa.text("SELECT collection_id FROM firestore_pg_collections ORDER BY collection_id")
        ).scalars().all()
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
    client = redis.Redis(host=host, port=port, socket_connect_timeout=5, socket_timeout=5)
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
    from utils.other import storage

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "profile.wav"
        _write_probe_wav(probe)
        uploaded_size = probe.stat().st_size
        url = storage.upload_profile_audio(str(probe), RUN_UID)
        if not url:
            raise CheckFailed("upload_profile_audio returned no URL")
        has_profile = storage.get_user_has_speech_profile(RUN_UID)
        if not has_profile:
            raise CheckFailed("uploaded profile audio is not visible through get_user_has_speech_profile")
        downloaded = storage.get_profile_audio_if_exists(RUN_UID, download=True)
        if not downloaded:
            raise CheckFailed("get_profile_audio_if_exists could not read the uploaded object back")
        downloaded_path = Path(downloaded)
        read_back = downloaded_path.stat().st_size if downloaded_path.exists() else 0
        # The shim stores the object verbatim, so the round trip is byte-exact.
        if read_back != uploaded_size:
            raise CheckFailed(f"round-tripped audio size {read_back} != uploaded size {uploaded_size}")
    return {
        "detail": f"uploaded, listed and downloaded {uploaded_size} bytes through the storage shim",
        "evidence": {"object_url": url, "bytes": uploaded_size, "uid": RUN_UID},
    }


def check_auth() -> dict:
    if not AUTH_DEV_ISSUER_SECRET:
        raise CheckFailed("AUTH_DEV_ISSUER_SECRET is not set for the verify process")

    status, payload = http(
        "POST",
        f"{AUTH_URL}/auth-issue",
        headers={"authorization": f"Bearer {AUTH_DEV_ISSUER_SECRET}"},
        body={"uid": RUN_UID},
    )
    if status != 200:
        raise CheckFailed(f"auth-server /auth-issue returned {status}: {payload[:300]}")
    token = json.loads(payload).get("token")
    if not token:
        raise CheckFailed("auth-server /auth-issue returned no token")

    unauthenticated_status, _ = http("GET", f"{BACKEND_URL}/v1/conversations?limit=1")
    if unauthenticated_status not in (401, 403):
        raise CheckFailed(
            "backend accepted an unauthenticated /v1/conversations request "
            f"(status {unauthenticated_status}) — the auth boundary is open"
        )

    authenticated_status, authenticated_body = http(
        "GET",
        f"{BACKEND_URL}/v1/conversations?limit=1",
        headers={"authorization": f"Bearer {token}"},
    )
    if authenticated_status != 200:
        raise CheckFailed(
            f"backend rejected a valid Better Auth token (status {authenticated_status}): {authenticated_body[:300]}"
        )
    return {
        "detail": "Better Auth JWT accepted, identical request without it refused",
        "evidence": {
            "uid": RUN_UID,
            "unauthenticated_status": unauthenticated_status,
            "authenticated_status": authenticated_status,
            "token_prefix": token[:12],
        },
    }


def check_queue() -> dict:
    """Prove the worker → backend dispatch contract, not just that Redis answers.

    The worker presents x-omi-queue-secret to the handler route, which verifies it
    with the same env var (backend/utils/cloud_tasks.py::_verify_redis_worker).
    A mismatch is silent at startup and only surfaces as 403s on real jobs, so
    this exercises both sides: an invalid payload carrying the right secret must
    get past authentication, and the wrong secret must be refused.
    """
    from utils import cloud_tasks_redis as queue

    if not queue.queue_enabled():
        raise CheckFailed("QUEUE_BACKEND is not redis for this process")

    missing = [alias for alias in queue.QUEUE_ALIASES if not queue.queue_dispatch_configured(alias)]
    if missing:
        names = ", ".join(queue.WORKER_SECRET_ENV[alias] for alias in missing)
        raise CheckFailed(f"queues without a handler URL + worker secret: {names}")

    secrets_by_queue = {alias: queue.worker_secret(alias) for alias in queue.QUEUE_ALIASES}
    sync_url = os.environ["SYNC_TASKS_HANDLER_URL"]
    accepted_status, _ = http(
        "POST",
        sync_url,
        headers={"x-omi-queue-name": "sync", "x-omi-queue-secret": secrets_by_queue["sync"]},
        body={},
    )
    if accepted_status in (401, 403):
        raise CheckFailed(
            f"handler rejected the secret the queue worker would present (status {accepted_status}) — "
            "worker and handler disagree on QUEUE_REDIS_SYNC_WORKER_SECRET"
        )
    refused_status, _ = http(
        "POST",
        sync_url,
        headers={"x-omi-queue-name": "sync", "x-omi-queue-secret": "definitely-not-the-secret"},
        body={},
    )
    if refused_status not in (401, 403):
        raise CheckFailed(f"handler accepted a wrong worker secret (status {refused_status})")

    worker_pid = None
    pid_file = Path(os.environ.get("OMI_LOCAL_STATE_DIR", "")) / "pids" / "queue-worker.pid"
    if pid_file.is_file():
        worker_pid = pid_file.read_text(encoding="utf-8").strip()
        try:
            os.kill(int(worker_pid), 0)
        except (OSError, ValueError) as error:
            raise CheckFailed(f"queue worker process {worker_pid} is not running: {error}") from error

    return {
        "detail": (
            f"{len(queue.QUEUE_ALIASES)} queues configured; the worker secret is accepted by the "
            "handler and a wrong one is refused"
        ),
        "evidence": {
            "queues": list(queue.QUEUE_ALIASES),
            "handler_status_with_secret": accepted_status,
            "handler_status_with_wrong_secret": refused_status,
            "worker_pid": worker_pid,
        },
    }


def check_backend() -> dict:
    status, payload = http("GET", f"{BACKEND_URL}/v1/health")
    if status != 200:
        raise CheckFailed(f"GET /v1/health returned {status}")
    return {"detail": f"GET /v1/health → {status}", "evidence": {"body": payload[:200]}}


CHECKS = (
    ("postgres", "firestore_pg shim over PostgreSQL", check_postgres),
    ("redis", "queue/cache backend", check_redis),
    ("storage", "storage shim over MinIO", check_storage),
    ("queue", "queue worker → backend dispatch contract", check_queue),
    ("auth", "Better Auth → backend auth boundary", check_auth),
    ("backend", "backend HTTP surface", check_backend),
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
            results.append(
                {
                    "check": name,
                    "description": description,
                    "status": "pass",
                    "detail": outcome.get("detail", ""),
                    "evidence": outcome.get("evidence", {}),
                    "seconds": round(time.monotonic() - began, 3),
                }
            )
            if not arguments.quiet:
                print(f"  PASS  {name:<9} {outcome.get('detail', '')}")
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

    failed = [result for result in results if result["status"] != "pass"]
    evidence = {
        "kind": "omi-local-dev-verify",
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "backend_url": BACKEND_URL,
        "auth_url": AUTH_URL,
        "uid": RUN_UID,
        "ok": not failed,
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
    print(f"local dev verify: PASSED ({len(results)}/{len(results)} checks)")
    if arguments.evidence:
        print(f"evidence: {arguments.evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
