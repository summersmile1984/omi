"""Task queue: Cloud Tasks -> Redis for operator-run deployments.

Upstream enqueues through `utils.cloud_tasks`. The fork swaps each enqueue
function for the Redis equivalent, so callers keep their signatures and upstream
keeps its file.
"""

from __future__ import annotations

from typing import Any, Callable, List

from ..registry import Patch

# upstream enqueue function -> fork implementation of the same shape
# Upstream and the fork name these identically, so the patch is a straight
# redirect rather than a mapping. Listing them explicitly (instead of scanning
# for "enqueue_*") means a new upstream enqueue function is not silently left
# on Cloud Tasks in a deployment that has none.
ENQUEUE_SEAMS = (
    "enqueue_sync_job",
    "enqueue_audio_merge_job",
    "enqueue_account_deletion_wipe",
    "enqueue_listen_finalization_job",
)


def _uses_redis_queue(profile: dict) -> bool:
    return profile.get("data_plane", {}).get("queue") == "redis"


def _redis_enqueue(fork_function_name: str) -> Callable[[Any], Any]:
    def build(original: Callable[..., Any]) -> Callable[..., Any]:
        def enqueue(*args: Any, **kwargs: Any) -> Any:
            from utils import cloud_tasks_redis

            return getattr(cloud_tasks_redis, fork_function_name)(*args, **kwargs)

        enqueue.__doc__ = f"fork: routes to cloud_tasks_redis.{fork_function_name}"
        return enqueue

    return build


def patches() -> List[Patch]:
    return [
        Patch(
            name=f"queue.{name}",
            module="utils.cloud_tasks",
            attribute=name,
            build=_redis_enqueue(name),
            applies_to=_uses_redis_queue,
            reason="operator-run deployments have no Cloud Tasks; the Redis worker takes the same payloads",
        )
        for name in ENQUEUE_SEAMS
    ] + [
        Patch(
            name="queue.worker-authentication",
            module="utils.cloud_tasks",
            attribute="_verify_redis_worker",
            build=_route_worker_auth,
            applies_to=_uses_redis_queue,
            reason="each Redis queue accepts only its configured worker credential",
        )
    ]


def _route_worker_auth(original: Callable[..., Any]) -> Callable[..., Any]:
    def verify(request: Any) -> int:
        import os
        import secrets

        from fastapi import HTTPException

        from ..queue_config import QUEUES

        queue = next((q for q in QUEUES if q.path == request.url.path), None)
        expected = os.environ.get(queue.secret_env, '') if queue is not None else ''
        presented = request.headers.get('x-omi-queue-secret', '')
        if len(expected) < 32 or not secrets.compare_digest(expected.encode(), presented.encode()):
            raise HTTPException(status_code=403, detail='Invalid Redis worker secret')
        return 0

    return verify
