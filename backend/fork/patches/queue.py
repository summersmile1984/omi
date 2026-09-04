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
    return (
        [
            Patch(
                name=f"queue.{name}",
                module="utils.cloud_tasks",
                attribute=name,
                build=_redis_enqueue(name),
                applies_to=_uses_redis_queue,
                reason="operator-run deployments have no Cloud Tasks; the Redis worker takes the same payloads",
            )
            for name in ENQUEUE_SEAMS
        ]
        + [
            Patch(
                name="queue.worker-authentication",
                module="utils.cloud_tasks",
                attribute="_verify_redis_worker",
                build=_route_worker_auth,
                applies_to=_uses_redis_queue,
                reason="each Redis queue accepts only its configured worker credential",
            )
        ]
        + _finalization_patches()
    )


def _finalization_patches():
    from .. import finalization_queue as owner
    from utils.cloud_tasks_redis import enqueue_listen_finalization_job

    targets = [
        ("utils.cloud_tasks", "enqueue_listen_finalization_job", enqueue_listen_finalization_job),
        ("utils.cloud_tasks", "is_listen_finalization_dispatch_enabled", owner.enabled),
        ("utils.cloud_tasks", "is_listen_finalization_dispatch_configured", owner.configured),
        ("utils.conversations.lifecycle", "enqueue_listen_finalization_job", enqueue_listen_finalization_job),
        ("utils.conversations.lifecycle", "is_listen_finalization_dispatch_enabled", owner.enabled),
        ("utils.conversations.lifecycle", "is_listen_finalization_dispatch_configured", owner.configured),
        ("services.conversation_finalization", "enqueue_listen_finalization_job", enqueue_listen_finalization_job),
        ("services.conversation_finalization", "is_listen_finalization_dispatch_enabled", owner.enabled),
        ("routers.listen.conversations", "is_listen_finalization_dispatch_enabled", owner.enabled),
    ]
    return [
        Patch(
            name="queue.finalization." + module + "." + attribute,
            module=module,
            attribute=attribute,
            build=lambda _, function=function: function,
            applies_to=_uses_redis_queue,
            reason="Redis dispatch admission and replay use the existing PG outbox/lease authority",
        )
        for module, attribute, function in targets
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
        # Only the authenticated, route-scoped worker may supply delivery state.
        # Existing queued envelopes/worker requests without a count start at zero.
        values = request.headers.getlist('x-omi-queue-retry-count')
        raw = values[0] if values else '0'
        if len(values) > 1 or not raw.isascii() or not raw.isdecimal() or len(raw) > 3:
            raise HTTPException(status_code=400, detail='Invalid Redis delivery attempt')
        retry_count = int(raw)
        if retry_count >= queue.max_attempts():
            raise HTTPException(status_code=400, detail='Redis delivery attempt exceeds its queue budget')
        return retry_count

    return verify
