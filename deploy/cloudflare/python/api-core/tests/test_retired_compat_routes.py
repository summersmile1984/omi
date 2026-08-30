import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from retired_compat_routes import (  # noqa: E402
    delete_limitless_conversations,
    migrate_ai_tasks,
    migrate_conversation_items,
    restore_legacy_conversation_items,
)


class FakeRequest:
    def __init__(self, env, headers=None, query_params=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.query_params = query_params or {}


def signed_headers(secret: str, uid: str = "compat-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "compat-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(secret: str):
    return type("Env", (), {"INTERNAL_ASSERTION_SECRET": secret})()


def test_retired_routes_preserve_inert_response_envelopes():
    secret = "compat-secret"
    env = make_env(secret)
    headers = signed_headers(secret)

    assert asyncio.run(migrate_ai_tasks(FakeRequest(env, headers))) == {
        "status": "legacy task migration retired; no action taken"
    }
    assert asyncio.run(migrate_conversation_items(FakeRequest(env, headers, {"limit": "100"}))) == {
        "status": "ok",
        "migrated": 0,
        "deleted": 0,
        "restored": 0,
        "skipped_existing": 0,
        "has_more": False,
        "next_cursor": None,
    }
    assert asyncio.run(restore_legacy_conversation_items(FakeRequest(env, headers, {"cursor": "page-1"}))) == {
        "status": "ok",
        "restored": 0,
        "skipped_existing": 0,
        "has_more": False,
        "next_cursor": None,
    }
    assert asyncio.run(delete_limitless_conversations(FakeRequest(env, headers))) == {
        "deleted_count": 0,
        "message": "Successfully deleted 0 Limitless conversations",
    }


def test_retired_routes_fail_closed_and_bound_pagination():
    secret = "compat-secret"
    env = make_env(secret)

    unauthorized = asyncio.run(migrate_ai_tasks(FakeRequest(env)))
    assert unauthorized.status_code == 401

    invalid_limit = asyncio.run(
        migrate_conversation_items(FakeRequest(env, signed_headers(secret), {"limit": "101"}))
    )
    assert invalid_limit.status_code == 422

    invalid_cursor = asyncio.run(
        restore_legacy_conversation_items(FakeRequest(env, signed_headers(secret), {"cursor": ""}))
    )
    assert invalid_cursor.status_code == 422

    invalid_limitless_auth = asyncio.run(delete_limitless_conversations(FakeRequest(env)))
    assert invalid_limitless_auth.status_code == 401
