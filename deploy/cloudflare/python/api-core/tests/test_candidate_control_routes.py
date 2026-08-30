import asyncio
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from candidate_control_routes import get_candidate_workflow_control  # noqa: E402


class FakeRequest:
    def __init__(self, env, headers=None):
        self.scope = {"env": env}
        self.headers = headers or {}


def signed_headers(secret: str, uid: str = "candidate-user"):
    raw = json.dumps({"uid": uid, "authority": "better-auth"}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def test_candidate_control_requires_authentication():
    env = type("Env", (), {"INTERNAL_ASSERTION_SECRET": "candidate-secret"})()
    response = asyncio.run(get_candidate_workflow_control(FakeRequest(env)))
    assert response.status_code == 401


def test_candidate_control_defaults_to_the_legacy_safe_shell():
    secret = "candidate-secret"
    env = type("Env", (), {"INTERNAL_ASSERTION_SECRET": secret})()
    assert asyncio.run(get_candidate_workflow_control(FakeRequest(env, signed_headers(secret)))) == {
        "workflow_mode": "off",
        "account_generation": 0,
        "chat_first_ui": False,
    }
