"""Real Redis fixtures, owned only by the explicit fork container lane."""

from collections.abc import Iterator

import pytest
import redis
from testcontainers.redis import RedisContainer


@pytest.fixture(scope='session')
def redis_container() -> Iterator[RedisContainer]:
    # Testcontainers waits for Redis readiness and stops the container on exit.
    with RedisContainer('redis:7-alpine') as container:
        yield container


@pytest.fixture
def redis_client(redis_container: RedisContainer) -> Iterator[redis.Redis]:
    with redis.Redis(
        host=redis_container.get_container_host_ip(),
        port=int(redis_container.get_exposed_port(6379)),
        socket_connect_timeout=5,
        socket_timeout=5,
    ) as client:
        yield client
