import asyncio
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from staged_task_compat_routes import (  # noqa: E402
    clear_staged_tasks,
    create_staged_task,
    delete_staged_task,
    list_staged_tasks,
    promote_staged_task,
    promote_staged_task_by_id,
    update_staged_scores,
)


class FakeRequest:
    def __init__(self, headers=None):
        self.scope = {"env": type("Env", (), {"INTERNAL_ASSERTION_SECRET": "secret"})()}
        self.headers = headers or {}


def signed_headers(uid="user-1"):
    payload = {"uid": uid, "aud": "api-core", "exp": 4_000_000_000, "iat": 1_700_000_000, "jti": "jti-1"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(hmac.new(b"secret", encoded.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature}


def test_staged_task_routes_fail_closed_without_authentication():
    request = FakeRequest()
    handlers = [
        lambda: clear_staged_tasks(request),
        lambda: delete_staged_task(request, "task-1"),
        lambda: list_staged_tasks(request),
        lambda: update_staged_scores(request),
        lambda: create_staged_task(request),
        lambda: promote_staged_task(request),
        lambda: promote_staged_task_by_id(request, "task-1"),
    ]
    assert [asyncio.run(handler()).status_code for handler in handlers] == [401] * 7


def test_staged_task_routes_return_feature_disabled_for_signed_context():
    request = FakeRequest(signed_headers())
    handlers = [
        lambda: clear_staged_tasks(request),
        lambda: delete_staged_task(request, "task-1"),
        lambda: list_staged_tasks(request),
        lambda: update_staged_scores(request),
        lambda: create_staged_task(request),
        lambda: promote_staged_task(request),
        lambda: promote_staged_task_by_id(request, "task-1"),
    ]
    assert [asyncio.run(handler()).status_code for handler in handlers] == [404] * 7
