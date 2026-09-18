"""Smoke tests for the testcontainers Redis fixture.

These tests prove that the testcontainers session fixture can spin up a real
``redis:7-alpine`` container on this machine, expose its port, and let a
Python ``redis.Redis`` client round-trip data through it. They intentionally
do not touch any backend module so the suite stays a self-contained
regression guard for the dev-harness container-orchestration story.

Run with:
    pytest -m container_integration tests/integration/containers/
or explicitly skip:
    pytest -m "not container_integration"
"""

from __future__ import annotations

import pytest
import redis

# pytestmark applies the marker so the test is selectable via -m even when the
# auto-mark in conftest.py is bypassed by file ordering.
pytestmark = pytest.mark.container_integration


def test_redis_container_is_running(redis_container) -> None:
    """The container fixture must hand back a live Docker container."""
    assert (
        redis_container.status == "running"
    ), f"expected redis container to be running, got status={redis_container.status!r}"


def test_redis_client_ping(redis_client: redis.Redis) -> None:
    """A standard PING must round-trip through the containerised Redis."""
    assert redis_client.ping() is True


def test_redis_client_set_and_get(redis_client: redis.Redis) -> None:
    """SET/GET must persist across the container boundary."""
    key = "omi-tests:smoke:hello"
    value = "world-from-testcontainers"

    redis_client.set(key, value, ex=30)
    assert redis_client.get(key) == value
    redis_client.delete(key)


def test_redis_client_namespace_isolation(redis_client: redis.Redis) -> None:
    """Each test's writes live in the same container but isolated via keys."""
    redis_client.set("omi-tests:smoke:a", "1")
    redis_client.set("omi-tests:smoke:b", "2")
    keys = sorted(redis_client.keys("omi-tests:smoke:*"))
    # Other tests may also write under this prefix in the same session; the
    # assertion only requires that both keys we just wrote are visible.
    assert "omi-tests:smoke:a" in keys
    assert "omi-tests:smoke:b" in keys
    redis_client.delete("omi-tests:smoke:a", "omi-tests:smoke:b")
