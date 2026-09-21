"""Qualify the backend's listen admission lock against real Redis semantics."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
import redis

from database import redis_db

pytestmark = pytest.mark.container_integration


def test_listen_lock_admits_one_contender_without_blocking_other_users(
    redis_client: redis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = f'container-{uuid4().hex}'
    other_uid = f'{uid}-other'
    keys = [f'users:{subject}:listen_rate_limit' for subject in (uid, other_uid)]
    monkeypatch.setattr(redis_db, 'r', redis_client)
    contenders = 8
    ready = Barrier(contenders)

    def acquire(_: int) -> bool:
        ready.wait(timeout=10)
        return redis_db.try_acquire_listen_lock(uid, ttl=60)

    try:
        with ThreadPoolExecutor(max_workers=contenders) as executor:
            admitted = list(executor.map(acquire, range(contenders)))
        assert admitted.count(True) == 1
        assert admitted.count(False) == contenders - 1
        assert 0 < redis_client.ttl(keys[0]) <= 60
        assert redis_db.try_acquire_listen_lock(other_uid, ttl=60) is True
        assert redis_db.try_acquire_listen_lock(uid, ttl=60) is False
    finally:
        redis_client.delete(*keys)
