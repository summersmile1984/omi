"""Redis-backed task queue shim — Cloud Tasks replacement for local dev.

Implements the same four enqueue entrypoints as utils/cloud_tasks.py but
writes to Redis lists (one per queue). The first three use named-task dedup;
finalization relies on its persisted PostgreSQL generation and lease:

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
import threading
import time
from typing import Any, Dict, Optional, Tuple, cast

import httpx
import redis

from fork.queue_config import QUEUES

logger = logging.getLogger(__name__)

QUEUE_ALIASES = tuple(queue.name for queue in QUEUES)

_client: Optional[redis.Redis] = None
_client_config: Optional[Tuple[str, int, Optional[str]]] = None
_client_lock = threading.Lock()


def _redis_config() -> Tuple[str, int, Optional[str]]:
    return (
        os.getenv("REDIS_DB_HOST", "127.0.0.1"),
        int(os.getenv("REDIS_DB_PORT", "6379") or "6379"),
        os.getenv("REDIS_DB_PASSWORD"),
    )


def _queue_names() -> Dict[str, str]:
    prefix = os.getenv("QUEUE_REDIS_PREFIX", "omi:queue")
    return {alias: f"{prefix}:{alias}" for alias in QUEUE_ALIASES}


def _r() -> redis.Redis:
    global _client, _client_config
    config = _redis_config()
    if _client is None or _client_config != config:
        with _client_lock:
            if _client is None or _client_config != config:
                _client = redis.Redis(host=config[0], port=config[1], password=config[2], decode_responses=True)
                _client_config = config
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
    task_id = str(payload.get("job_id") or "")
    if not task_id:
        raise ValueError("job_id must be non-empty")
    _enqueue(_queue_names()["sync"], task_id, payload)


def enqueue_audio_merge_job(payload: Dict[str, Any]) -> None:
    if payload.get("schema_version") == 2:
        task_id = f"amc-{payload['conversation_id']}-{payload['fingerprint']}"
    else:
        task_id = f"am-{payload['conversation_id']}-{payload['audio_file_id']}"
    _enqueue(_queue_names()["audio-merge"], task_id, payload)


def enqueue_account_deletion_wipe(wipe_job_id: str) -> None:
    if not wipe_job_id:
        raise ValueError("wipe_job_id must be non-empty")
    _enqueue(_queue_names()["account-deletion"], f"wipe-{wipe_job_id}", {"job_id": wipe_job_id})


def enqueue_listen_finalization_job(job_id: str, dispatch_generation: int) -> None:
    if not isinstance(job_id, str) or not job_id or type(dispatch_generation) is not int or dispatch_generation < 1:
        raise ValueError("finalization requires job identity and a positive dispatch generation")
    next(queue for queue in QUEUES if queue.name == "finalization").validate()
    # PostgreSQL's persisted generation and lease own duplicate handling. One
    # Redis command publishes the opaque identity; an uncertain reply is safe
    # to replay. A permanent names set would swallow later dispatch generations.
    _r().rpush(
        _queue_names()["finalization"],
        json.dumps(
            {
                "task_id": f"fin-{job_id}-{dispatch_generation}",
                "payload": {"job_id": job_id, "dispatch_generation": dispatch_generation},
            }
        ),
    )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

HANDLER_URL_ENV = {queue.name: queue.handler_env for queue in QUEUES}


def _worker(queue_name: str) -> None:
    queue_names = _queue_names()
    if queue_name not in queue_names:
        logger.error("unknown queue %s (choices: %s)", queue_name, ", ".join(queue_names))
        return
    queue_key = queue_names[queue_name]
    queue = next(queue for queue in QUEUES if queue.name == queue_name)
    max_attempts = queue.max_attempts()
    handler_env = HANDLER_URL_ENV[queue_name]
    handler_url = os.getenv(handler_env, "")
    worker_secret = os.getenv("QUEUE_REDIS_WORKER_SECRET", "")
    if not handler_url or not worker_secret:
        logger.error("%s and QUEUE_REDIS_WORKER_SECRET are required to dispatch %s tasks", handler_env, queue_name)
        return
    logger.info("worker %s -> %s (blocking on %s)", queue_name, handler_url, queue_key)
    r = _r()
    while True:
        raw = cast(Any, r).blpop([queue_key], timeout=1)
        if raw is None:
            continue
        try:
            item = json.loads(raw[1])
            retry_count = item.get('retry_count', 0)
            if (
                not isinstance(item, dict)
                or not isinstance(item.get('payload'), dict)
                or not isinstance(item.get('task_id'), str)
                or type(retry_count) is not int
                or not 0 <= retry_count < max_attempts
            ):
                raise ValueError('invalid delivery envelope')
        except (ValueError, TypeError, AttributeError):
            r.rpush(queue_key + ':dead-letter', raw[1])
            logger.error('worker %s parked an invalid delivery envelope', queue_name)
            continue
        try:
            resp = httpx.post(
                handler_url,
                json=item["payload"],
                headers={"X-Omi-Queue-Secret": worker_secret, 'X-Omi-Queue-Retry-Count': str(retry_count)},
                timeout=queue.request_timeout(),
            )
            logger.info("task %s -> %s status=%s", item.get("task_id"), handler_url, resp.status_code)
            # Finalization 409 means a held lease or completion conflict. A
            # 200 dropped/acked/dead_letter is the handler's terminal receipt.
            # Other 4xx and redirects must retain the task, never discard it as
            # a successful write (for example a mismatched worker credential).
            retry = resp.status_code >= 500 or resp.status_code in (409, 429)
            if not retry and not 200 <= resp.status_code < 300:
                item['delivery_failure'] = f'http_{resp.status_code}'
                r.rpush(queue_key + ':dead-letter', json.dumps(item))
                logger.error('worker %s parked a rejected delivery status=%s', queue_name, resp.status_code)
                continue
        except httpx.HTTPError:
            # Do not log response bodies, credentials or full request exceptions.
            logger.warning('worker %s delivery transport failed', queue_name)
            retry = True
        if retry:
            item['retry_count'] = retry_count + 1
            exhausted = item['retry_count'] >= max_attempts
            destination = queue_key + ':dead-letter' if exhausted else queue_key
            r.rpush(destination, json.dumps(item))
            if exhausted:
                logger.error('worker %s exhausted its delivery budget; envelope retained in dead-letter', queue_name)
                continue
            time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Redis task queue worker")
    parser.add_argument("--worker", choices=list(QUEUE_ALIASES), help="queue to consume")
    parser.add_argument("--all", action="store_true", help="run workers for every queue")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.all:
        threads = [threading.Thread(target=_worker, args=(q,), daemon=True) for q in QUEUE_ALIASES]
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
