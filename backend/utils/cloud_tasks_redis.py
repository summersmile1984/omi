"""Durable Redis task queues with Cloud Tasks-compatible delivery semantics.

Each task moves through ``ready`` and ``pending`` sorted sets while its payload
lives in a Redis hash. Enqueue, claim, acknowledgement, retry, and dead-letter
transitions use optimistic Redis transactions so a worker crash cannot lose a
task and concurrent enqueues preserve named-task deduplication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple, cast

import httpx
import redis
from redis.exceptions import WatchError

logger = logging.getLogger(__name__)

QUEUE_ALIASES = ("sync", "audio-merge", "account-deletion", "finalization")

HANDLER_URL_ENV = {
    "sync": "SYNC_TASKS_HANDLER_URL",
    "audio-merge": "AUDIO_MERGE_HANDLER_URL",
    "account-deletion": "ACCOUNT_DELETION_HANDLER_URL",
    "finalization": "LISTEN_FINALIZATION_TASKS_HANDLER_URL",
}
WORKER_SECRET_ENV = {
    "sync": "QUEUE_REDIS_SYNC_WORKER_SECRET",
    "audio-merge": "QUEUE_REDIS_AUDIO_MERGE_WORKER_SECRET",
    "account-deletion": "QUEUE_REDIS_ACCOUNT_DELETION_WORKER_SECRET",
    "finalization": "QUEUE_REDIS_FINALIZATION_WORKER_SECRET",
}

_client: Optional[redis.Redis] = None
_client_config: Optional[Tuple[str, str, str, str, str]] = None
_client_lock = threading.Lock()


@dataclass(frozen=True)
class ClaimedTask:
    queue_name: str
    queue_key: str
    token: str
    task_key: str
    task_id: str
    payload: Dict[str, Any]
    retry_count: int
    lease_owner: str


def _redis_config() -> Tuple[str, str, str, str, str]:
    return (
        os.getenv("QUEUE_REDIS_URL", "").strip(),
        os.getenv("REDIS_DB_HOST", "127.0.0.1"),
        os.getenv("REDIS_DB_PORT", "6379") or "6379",
        os.getenv("REDIS_DB_PASSWORD", ""),
        os.getenv("QUEUE_REDIS_DB", "0") or "0",
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
                if config[0]:
                    _client = redis.Redis.from_url(config[0], decode_responses=True)
                else:
                    _client = redis.Redis(
                        host=config[1],
                        port=int(config[2]),
                        password=config[3] or None,
                        db=int(config[4]),
                        decode_responses=True,
                    )
                _client_config = config
    return _client


def queue_enabled() -> bool:
    return os.getenv("QUEUE_BACKEND", "").strip().lower() == "redis"


def worker_secret(queue_name: str) -> str:
    env_name = WORKER_SECRET_ENV.get(queue_name)
    if not env_name:
        return ""
    secret = os.getenv(env_name, "").strip()
    return secret


def queue_dispatch_configured(queue_name: str) -> bool:
    handler_env = HANDLER_URL_ENV.get(queue_name)
    return bool(handler_env and os.getenv(handler_env, "").strip() and worker_secret(queue_name))


def _task_token(task_id: str) -> str:
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()


def _task_key(queue_key: str, token: str) -> str:
    return f"{queue_key}:task:{token}"


def _ready_key(queue_key: str) -> str:
    return f"{queue_key}:ready"


def _pending_key(queue_key: str) -> str:
    return f"{queue_key}:pending"


def _dlq_key(queue_key: str) -> str:
    return f"{queue_key}:dlq"


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, value)


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(0.001, value)


def _max_attempts(queue_name: str) -> int:
    if queue_name == "account-deletion":
        return _positive_int_env("ACCOUNT_DELETION_TASKS_MAX_ATTEMPTS", _positive_int_env("SYNC_TASKS_MAX_ATTEMPTS", 5))
    if queue_name == "finalization":
        return _positive_int_env(
            "LISTEN_FINALIZATION_TASKS_MAX_ATTEMPTS", _positive_int_env("SYNC_TASKS_MAX_ATTEMPTS", 5)
        )
    return _positive_int_env("SYNC_TASKS_MAX_ATTEMPTS", 5)


def _enqueue(queue_key: str, task_id: str, payload: Dict[str, Any], *, now_ms: Optional[int] = None) -> bool:
    """Atomically create a named task and make it ready; duplicates are a no-op."""
    client = _r()
    token = _task_token(task_id)
    task_key = _task_key(queue_key, token)
    ready_key = _ready_key(queue_key)
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    while True:
        try:
            with client.pipeline() as pipe:
                pipe.watch(task_key)
                if pipe.exists(task_key):
                    pipe.unwatch()
                    logger.info("redis queue %s task %s already present, skipping", queue_key, task_id)
                    return False
                pipe.multi()
                pipe.hset(
                    task_key,
                    mapping={
                        "task_id": task_id,
                        "payload": json.dumps(payload, separators=(",", ":")),
                        "retry_count": "0",
                        "created_at_ms": str(timestamp),
                    },
                )
                pipe.zadd(ready_key, {token: timestamp})
                pipe.execute()
                logger.info("redis queue %s enqueued task %s", queue_key, task_id)
                return True
        except WatchError:
            continue


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
    if not job_id:
        raise ValueError("job_id must be non-empty")
    _enqueue(
        QUEUE_NAMES["finalization"], f"fin-{job_id}", {"job_id": job_id, "dispatch_generation": dispatch_generation}
    )


def _recover_expired(queue_key: str, now_ms: int, *, limit: int = 100) -> int:
    client = _r()
    ready_key = _ready_key(queue_key)
    pending_key = _pending_key(queue_key)
    while True:
        try:
            with client.pipeline() as pipe:
                pipe.watch(pending_key)
                expired = cast(list[str], pipe.zrangebyscore(pending_key, "-inf", now_ms, start=0, num=limit))
                if not expired:
                    pipe.unwatch()
                    return 0
                pipe.multi()
                for token in expired:
                    task_key = _task_key(queue_key, token)
                    pipe.zrem(pending_key, token)
                    pipe.zadd(ready_key, {token: now_ms})
                    pipe.hdel(task_key, "lease_owner", "lease_until_ms")
                pipe.execute()
                logger.warning("redis queue %s recovered %s expired leases", queue_key, len(expired))
                return len(expired)
        except WatchError:
            continue


def _claim_task(queue_name: str, *, now_ms: Optional[int] = None) -> Optional[ClaimedTask]:
    queue_key = _queue_names()[queue_name]
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    _recover_expired(queue_key, timestamp)
    client = _r()
    ready_key = _ready_key(queue_key)
    pending_key = _pending_key(queue_key)
    lease_ms = int(_positive_float_env("QUEUE_REDIS_LEASE_SECONDS", 1800.0) * 1000)
    while True:
        try:
            with client.pipeline() as pipe:
                pipe.watch(ready_key, pending_key)
                due = cast(list[str], pipe.zrangebyscore(ready_key, "-inf", timestamp, start=0, num=1))
                if not due:
                    pipe.unwatch()
                    return None
                token = due[0]
                task_key = _task_key(queue_key, token)
                pipe.watch(task_key)
                task = cast(Dict[str, str], pipe.hgetall(task_key))
                if not task:
                    pipe.multi()
                    pipe.zrem(ready_key, token)
                    pipe.execute()
                    continue
                lease_owner = uuid.uuid4().hex
                lease_until_ms = timestamp + lease_ms
                pipe.multi()
                pipe.zrem(ready_key, token)
                pipe.zadd(pending_key, {token: lease_until_ms})
                pipe.hset(task_key, mapping={"lease_owner": lease_owner, "lease_until_ms": str(lease_until_ms)})
                pipe.execute()
                return ClaimedTask(
                    queue_name=queue_name,
                    queue_key=queue_key,
                    token=token,
                    task_key=task_key,
                    task_id=task["task_id"],
                    payload=json.loads(task["payload"]),
                    retry_count=int(task.get("retry_count", "0")),
                    lease_owner=lease_owner,
                )
        except WatchError:
            continue


def _ack_task(task: ClaimedTask) -> bool:
    client = _r()
    pending_key = _pending_key(task.queue_key)
    while True:
        try:
            with client.pipeline() as pipe:
                pipe.watch(task.task_key)
                if pipe.hget(task.task_key, "lease_owner") != task.lease_owner:
                    pipe.unwatch()
                    return False
                pipe.multi()
                pipe.zrem(pending_key, task.token)
                pipe.delete(task.task_key)
                pipe.execute()
                return True
        except WatchError:
            continue


def _retry_delay_seconds(retry_count: int) -> float:
    base = _positive_float_env("QUEUE_REDIS_RETRY_BASE_SECONDS", 1.0)
    maximum = _positive_float_env("QUEUE_REDIS_RETRY_MAX_SECONDS", 60.0)
    return min(maximum, base * (2**retry_count))


def _fail_task(task: ClaimedTask, error: str, *, retryable: bool, now_ms: Optional[int] = None) -> str:
    client = _r()
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    pending_key = _pending_key(task.queue_key)
    ready_key = _ready_key(task.queue_key)
    next_retry_count = task.retry_count + 1
    should_retry = retryable and next_retry_count < _max_attempts(task.queue_name)
    while True:
        try:
            with client.pipeline() as pipe:
                pipe.watch(task.task_key)
                if pipe.hget(task.task_key, "lease_owner") != task.lease_owner:
                    pipe.unwatch()
                    return "stale"
                pipe.multi()
                pipe.zrem(pending_key, task.token)
                if should_retry:
                    available_at_ms = timestamp + int(_retry_delay_seconds(task.retry_count) * 1000)
                    pipe.hset(
                        task.task_key,
                        mapping={"retry_count": str(next_retry_count), "last_error": error[:512]},
                    )
                    pipe.hdel(task.task_key, "lease_owner", "lease_until_ms")
                    pipe.zadd(ready_key, {task.token: available_at_ms})
                else:
                    pipe.rpush(
                        _dlq_key(task.queue_key),
                        json.dumps(
                            {
                                "task_id": task.task_id,
                                "payload": task.payload,
                                "retry_count": next_retry_count,
                                "last_error": error[:512],
                                "failed_at_ms": timestamp,
                            },
                            separators=(",", ":"),
                        ),
                    )
                    pipe.delete(task.task_key)
                pipe.execute()
                return "retry" if should_retry else "dead_letter"
        except WatchError:
            continue


def _process_one(
    queue_name: str,
    *,
    http_post: Callable[..., Any] = httpx.post,
    now_ms: Optional[int] = None,
) -> bool:
    if queue_name not in QUEUE_ALIASES:
        raise ValueError(f"unknown queue: {queue_name}")
    task = _claim_task(queue_name, now_ms=now_ms)
    if task is None:
        return False
    handler_url = os.getenv(HANDLER_URL_ENV[queue_name], "").strip()
    secret = worker_secret(queue_name)
    if not handler_url or not secret:
        _fail_task(task, "worker_configuration_missing", retryable=False, now_ms=now_ms)
        return True
    headers = {
        "X-Omi-Queue-Secret": secret,
        "X-Omi-Queue-Name": queue_name,
        "X-CloudTasks-TaskRetryCount": str(task.retry_count),
    }
    try:
        response = http_post(
            handler_url,
            json=task.payload,
            headers=headers,
            timeout=_positive_float_env("QUEUE_REDIS_HTTP_TIMEOUT_SECONDS", 1500.0),
        )
    except Exception as exc:
        outcome = _fail_task(task, type(exc).__name__, retryable=True, now_ms=now_ms)
        logger.warning("redis queue %s task %s transport failure outcome=%s", queue_name, task.task_id, outcome)
        return True

    status_code = int(response.status_code)
    if 200 <= status_code < 300:
        _ack_task(task)
        logger.info("redis queue %s task %s acknowledged status=%s", queue_name, task.task_id, status_code)
    elif status_code in {409, 429} or status_code >= 500:
        outcome = _fail_task(task, f"http_{status_code}", retryable=True, now_ms=now_ms)
        logger.warning(
            "redis queue %s task %s retryable status=%s outcome=%s", queue_name, task.task_id, status_code, outcome
        )
    else:
        _fail_task(task, f"http_{status_code}", retryable=False, now_ms=now_ms)
        logger.warning("redis queue %s task %s dead-lettered status=%s", queue_name, task.task_id, status_code)
    return True


def _worker(queue_name: str) -> None:
    if queue_name not in QUEUE_ALIASES:
        logger.error("unknown queue %s (choices: %s)", queue_name, ", ".join(QUEUE_ALIASES))
        return
    if not queue_dispatch_configured(queue_name):
        logger.error(
            "%s and %s are required to dispatch %s tasks",
            HANDLER_URL_ENV[queue_name],
            WORKER_SECRET_ENV[queue_name],
            queue_name,
        )
        return
    logger.info("worker %s -> %s (blocking on %s)", queue_name, handler_url, queue_key)
    # redis-py exposes both synchronous and asyncio client overloads through
    # the same stubs. This worker is intentionally synchronous; keep the
    # boundary typed as Any so pyright does not infer an awaitable result from
    # the async overload while the runtime still receives a (key, payload)
    # tuple from ``Redis.blpop``.
    r: Any = _r()
    while True:
        if not _process_one(queue_name):
            time.sleep(poll_seconds)


def _run_all_workers() -> None:
    """Supervise all queue loops as one container failure domain.

    A returned or crashed child means one production queue is no longer being
    consumed.  Surface that to PID 1 immediately so Compose can mark the
    container unhealthy/restart it; joining the remaining infinite threads
    would otherwise hide the partial outage forever.
    """

    failures: queue.SimpleQueue[tuple[str, BaseException | None]] = queue.SimpleQueue()

    def run(queue_name: str) -> None:
        error: BaseException | None = None
        try:
            _worker(queue_name)
        except BaseException as exc:
            error = exc
        finally:
            failures.put((queue_name, error))

    for queue_name in QUEUE_ALIASES:
        threading.Thread(
            target=run,
            args=(queue_name,),
            daemon=True,
            name=f'omi-queue-{queue_name}',
        ).start()
    queue_name, error = failures.get()
    if error is None:
        raise RuntimeError(f'redis queue worker {queue_name} exited unexpectedly')
    raise RuntimeError(f'redis queue worker {queue_name} crashed') from error


def main() -> None:
    parser = argparse.ArgumentParser(description="Redis task queue worker")
    parser.add_argument("--worker", choices=list(QUEUE_ALIASES), help="queue to consume")
    parser.add_argument("--all", action="store_true", help="run workers for every queue")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.all:
        _run_all_workers()
    elif args.worker:
        _worker(args.worker)
    else:
        parser.error("--worker or --all required")


if __name__ == "__main__":
    main()
