"""Redis-backed task queue shim — Cloud Tasks replacement for local dev.

Implements the same four enqueue entrypoints as utils/cloud_tasks.py but
writes to Redis lists (one per queue) with an SADD name-set for the named-task
dedup semantics Cloud Tasks provides:

  - enqueue_sync_job(payload)            queue: omi:queue:sync
  - enqueue_audio_merge_job(payload)     queue: omi:queue:audio-merge
  - enqueue_account_deletion_wipe(id)    queue: omi:queue:account-deletion
  - enqueue_listen_finalization_job(...) queue: omi:queue:finalization

A worker (python -m utils.cloud_tasks_redis --worker <queue>) pops tasks and
POSTs them to the configured handler URL — the same handler endpoints the
backend already ships (/v2/sync-jobs/run, /v2/audio-merge-jobs/run, ...).

The dispatch-mode gates in cloud_tasks.py still apply: callers use this only
when QUEUE_BACKEND=redis and the corresponding *_DISPATCH_MODE is enabled.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import redis

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_DB_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_DB_PORT", "6379") or "6379")
REDIS_PASSWORD = os.getenv("REDIS_DB_PASSWORD")
REDIS_PREFIX = os.getenv("QUEUE_REDIS_PREFIX", "omi:queue")

QUEUE_NAMES = {
    "sync": f"{REDIS_PREFIX}:sync",
    "audio-merge": f"{REDIS_PREFIX}:audio-merge",
    "account-deletion": f"{REDIS_PREFIX}:account-deletion",
    "finalization": f"{REDIS_PREFIX}:finalization",
}

_client: Optional[redis.Redis] = None


def _r() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            decode_responses=True,
        )
    return _client


def queue_enabled() -> bool:
    return os.getenv("QUEUE_BACKEND", "").strip().lower() == "redis"


def _enqueue(queue_key: str, task_id: str, payload: Dict[str, Any]) -> None:
    """Push a task; duplicate task_id is a no-op (named-task dedup)."""
    r = _r()
    added = r.sadd(f"{queue_key}:names", task_id)
    if added:
        r.rpush(queue_key, json.dumps({"task_id": task_id, "payload": payload}))
        logger.info("redis queue %s enqueued task %s", queue_key, task_id)
    else:
        logger.info("redis queue %s task %s already present, skipping", queue_key, task_id)


# ---------------------------------------------------------------------------
# Same entrypoints as utils/cloud_tasks.py
# ---------------------------------------------------------------------------


def enqueue_sync_job(payload: Dict[str, Any]) -> None:
    _enqueue(QUEUE_NAMES["sync"], str(payload.get("job_id") or payload.get("id") or ""), payload)


def enqueue_audio_merge_job(payload: Dict[str, Any]) -> None:
    if payload.get("schema_version") == 2:
        task_id = f"amc-{payload['conversation_id']}-{payload['fingerprint']}"
    else:
        task_id = f"am-{payload['conversation_id']}-{payload['audio_file_id']}"
    _enqueue(QUEUE_NAMES["audio-merge"], task_id, payload)


def enqueue_account_deletion_wipe(wipe_job_id: str) -> None:
    _enqueue(QUEUE_NAMES["account-deletion"], f"wipe-{wipe_job_id}", {"wipe_job_id": wipe_job_id})


def enqueue_listen_finalization_job(job_id: str, dispatch_generation: int) -> None:
    _enqueue(
        QUEUE_NAMES["finalization"], f"fin-{job_id}", {"job_id": job_id, "dispatch_generation": dispatch_generation}
    )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

HANDLER_URL_ENV = {
    "sync": "SYNC_TASKS_HANDLER_URL",
    "audio-merge": "AUDIO_MERGE_HANDLER_URL",
    "account-deletion": "ACCOUNT_DELETION_HANDLER_URL",
    "finalization": "LISTEN_FINALIZATION_HANDLER_URL",
}


def _worker(queue_name: str) -> None:
    import httpx

    if queue_name not in QUEUE_NAMES:
        logger.error("unknown queue %s (choices: %s)", queue_name, ", ".join(QUEUE_NAMES))
        return
    queue_key = QUEUE_NAMES[queue_name]
    handler_env = HANDLER_URL_ENV[queue_name]
    handler_url = os.getenv(handler_env, "")
    if not handler_url:
        logger.error("%s not set; cannot dispatch %s tasks", handler_env, queue_name)
        return
    logger.info("worker %s -> %s (blocking on %s)", queue_name, handler_url, queue_key)
    # redis-py exposes both synchronous and asyncio client overloads through
    # the same stubs. This worker is intentionally synchronous; keep the
    # boundary typed as Any so pyright does not infer an awaitable result from
    # the async overload while the runtime still receives a (key, payload)
    # tuple from ``Redis.blpop``.
    r: Any = _r()
    while True:
        raw = r.blpop(queue_key, timeout=1)
        if raw is None:
            continue
        try:
            item = json.loads(raw[1])
            resp = httpx.post(handler_url, json=item["payload"], timeout=30.0)
            logger.info("task %s -> %s status=%s", item.get("task_id"), handler_url, resp.status_code)
            if resp.status_code >= 500:
                # requeue for retry (bounded by caller retry logic)
                r.rpush(queue_key, raw[1])
                time.sleep(1)
        except Exception as exc:  # pragma: no cover - worker resilience
            logger.error("worker %s task failed: %s", queue_name, exc)
            r.rpush(queue_key, raw[1])
            time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Redis task queue worker")
    parser.add_argument("--worker", choices=list(QUEUE_NAMES), help="queue to consume")
    parser.add_argument("--all", action="store_true", help="run workers for every queue")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.all:
        import threading

        threads = [threading.Thread(target=_worker, args=(q,), daemon=True) for q in QUEUE_NAMES]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    elif args.worker:
        _worker(args.worker)
    else:
        parser.error("--worker or --all required")


if __name__ == "__main__":
    main()
