import asyncio
import json
from types import SimpleNamespace

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from metrics_routes import get_metrics  # noqa: E402


class FakeRequest:
    def __init__(self, env, headers=None):
        self.scope = {"env": env}
        self.headers = headers or {}


def _body(response):
    return json.loads(response.body.decode("utf-8"))


def test_metrics_boundary_requires_the_operational_bearer_secret():
    env = SimpleNamespace(METRICS_SECRET="metrics-test-secret")

    missing = asyncio.run(get_metrics(FakeRequest(env)))
    assert missing.status_code == 401
    assert _body(missing) == {"detail": "Unauthorized"}

    wrong = asyncio.run(get_metrics(FakeRequest(env, {"authorization": "Bearer wrong"})))
    assert wrong.status_code == 401
    assert _body(wrong) == {"detail": "Unauthorized"}


def test_metrics_boundary_fails_closed_without_prometheus_authority():
    env = SimpleNamespace(METRICS_SECRET="metrics-test-secret")
    response = asyncio.run(get_metrics(FakeRequest(env, {"authorization": "Bearer metrics-test-secret"})))

    assert response.status_code == 503
    assert _body(response) == {"error": "metrics_unavailable"}
    assert response.headers["cache-control"] == "no-store"
    assert b"# HELP" not in response.body


def test_metrics_boundary_does_not_disclose_or_accept_when_secret_is_unconfigured():
    response = asyncio.run(
        get_metrics(FakeRequest(SimpleNamespace(), {"authorization": "Bearer caller-supplied"}))
    )

    assert response.status_code == 503
    assert _body(response) == {"error": "metrics_unavailable"}
