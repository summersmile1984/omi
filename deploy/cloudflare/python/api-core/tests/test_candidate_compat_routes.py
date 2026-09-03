import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from candidate_compat_routes import (  # noqa: E402
    accept_candidate,
    create_candidate,
    drain_candidate_integrations,
    expire_candidate,
    get_candidate,
    list_candidates,
    migrate_staged_candidates,
    reject_candidate,
)


class FakeRequest:
    def __init__(self, headers):
        self.scope = {"env": type("Env", (), {"INTERNAL_ASSERTION_SECRET": "candidate-secret"})()}
        self.headers = headers


def signed_headers():
    encoded = base64.urlsafe_b64encode(
        json.dumps(
            {"uid": "candidate-user", "authority": "better-auth", "requestId": "candidate-test"},
            separators=(",", ":"),
        ).encode()
    ).decode().rstrip("=")
    signature = hmac.new(b"candidate-secret", encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def response_payload(response):
    return json.loads(response.body)


def test_candidate_routes_require_the_edge_auth_context():
    request = FakeRequest({})
    response = asyncio.run(list_candidates(request))
    assert response.status_code == 401
    assert response_payload(response) == {"error": "unauthorized"}


def test_candidate_routes_fail_closed_when_the_feature_is_disabled():
    request = FakeRequest(signed_headers())
    handlers = [
        lambda: list_candidates(request),
        lambda: get_candidate(request, "candidate-1"),
        lambda: create_candidate(request),
        lambda: migrate_staged_candidates(request),
        lambda: drain_candidate_integrations(request),
        lambda: accept_candidate(request, "candidate-1"),
        lambda: reject_candidate(request, "candidate-1"),
        lambda: expire_candidate(request, "candidate-1"),
    ]
    for handler in handlers:
        response = asyncio.run(handler())
        assert response.status_code == 404
        assert response_payload(response) == {"detail": "Not found"}
