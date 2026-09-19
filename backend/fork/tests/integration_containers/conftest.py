"""Shared fixtures for testcontainers-based integration tests.

These fixtures spin up throwaway Docker containers at the start of the test
session and tear them down when pytest tears the session down. The harness
deliberately avoids backend/Firebase init (see ../conftest.py) so a
``pytest tests/integration/containers/`` invocation is fully hermetic: no
live Firestore, no live project credentials, just Docker.

Pinned image versions match the services the dev harness exposes so that
container-fixture behaviour matches what mobile / desktop clients see in
``make dev-up``:

- ``redis:7-alpine`` — same image and tag the Omi production stack pins.
- ``typesense/typesense:27.1`` — matches ``scripts/dev-harness/dev_harness/config.py``.
"""

from __future__ import annotations

import os
import socket
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import redis
from testcontainers.redis import RedisContainer


def _container_alive(container: Any) -> bool:
    """Return True iff the testcontainers container is still running."""
    try:
        return container.status == "running"
    except Exception:  # noqa: BLE001 — testcontainers raises on stopped container
        return False


def _wait_for_tcp(host: str, port: int, timeout: float = 15.0) -> None:
    """Block until ``host:port`` accepts a TCP connection or raise."""
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
    raise TimeoutError(
        f"Container at {host}:{port} never opened within {timeout:.1f}s " f"(last error: {last_error!r})"
    )


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    """Session-scoped Redis 7 Alpine container with a unique name per run."""
    name = f"omi-tests-redis-{uuid.uuid4().hex[:8]}"
    container = RedisContainer("redis:7-alpine").with_name(name)
    container.start()
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(6379))
        _wait_for_tcp(host, port)
        yield container
    finally:
        if _container_alive(container):
            container.stop()


@pytest.fixture
def redis_client(redis_container: RedisContainer) -> Iterator[redis.Redis]:
    """Function-scoped Redis client wired to the session container."""
    host = redis_container.get_container_host_ip()
    port = int(redis_container.get_exposed_port(6379))
    client = redis.Redis(host=host, port=port, decode_responses=True)
    try:
        yield client
    finally:
        client.close()


def pytest_configure(config: pytest.Config) -> None:
    """Allow selecting this sub-suite explicitly:
    ``pytest -m container_integration tests/integration/containers/``.
    """
    config.addinivalue_line(
        "markers",
        "container_integration: testcontainers-backed integration test (Docker required)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-mark every test in this directory so ``-m container_integration`` works."""
    for item in items:
        if "fork/tests/integration_containers" in str(item.fspath):
            item.add_marker(pytest.mark.container_integration)


# Skip the whole sub-suite when Docker isn't reachable so CI on a no-docker
# runner degrades gracefully (test discovery still succeeds).
REQUIRED_ENV = "OMI_REQUIRE_DOCKER_FOR_CONTAINER_TESTS"


def pytest_report_header(config: pytest.Config) -> list[str]:
    if not os.environ.get("DOCKER_HOST") and not os.path.exists("/var/run/docker.sock"):
        # macOS Docker Desktop uses ~/.docker/run/docker.sock; fall back to the
        # docker CLI to decide whether the daemon is reachable.
        import shutil
        import subprocess

        if shutil.which("docker"):
            try:
                subprocess.run(
                    ["docker", "info"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                return ["testcontainers: docker CLI reachable"]
            except (subprocess.SubprocessError, OSError):
                pass
        return ["testcontainers: docker daemon NOT detected — tests will be skipped"]
    return ["testcontainers: docker socket present"]
