import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from task_intelligence_compat_routes import (  # noqa: E402
    get_evaluation_debug_projection,
    get_what_matters_now,
)


class FakeRequest:
    def __init__(self, headers):
        self.scope = {"env": type("Env", (), {"INTERNAL_ASSERTION_SECRET": "task-secret"})()}
        self.headers = headers


def signed_headers():
    encoded = base64.urlsafe_b64encode(
        json.dumps(
            {"uid": "task-user", "authority": "better-auth", "requestId": "task-test"},
            separators=(",", ":"),
        ).encode()
    ).decode().rstrip("=")
    signature = hmac.new(b"task-secret", encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def response_payload(response):
    return json.loads(response.body)


def test_task_intelligence_reads_require_the_edge_auth_context():
    request = FakeRequest({})
    assert asyncio.run(get_what_matters_now(request)).status_code == 401
    assert asyncio.run(get_evaluation_debug_projection(request, "evaluation-1")).status_code == 401


def test_task_intelligence_reads_fail_closed_without_a_candidate_projection():
    request = FakeRequest(signed_headers())
    for response in (
        asyncio.run(get_what_matters_now(request)),
        asyncio.run(get_evaluation_debug_projection(request, "evaluation-1")),
    ):
        assert response.status_code == 404
        assert response_payload(response) == {"detail": "Not found"}
