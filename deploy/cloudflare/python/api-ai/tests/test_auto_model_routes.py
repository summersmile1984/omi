import asyncio
import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import auto_model_routes as routes  # noqa: E402


class FakeDb:
    def __init__(self):
        self.row = None

    def prepare(self, sql):
        return FakeStatement(self, sql)


class FakeStatement:
    def __init__(self, db, sql):
        self.db = db
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def first(self):
        assert self.sql.startswith("SELECT provider")
        return self.db.row

    async def run(self):
        assert self.sql.startswith("INSERT INTO cf_auto_model_pick")
        provider, detail_json, updated_at = self.args
        self.db.row = {"provider": provider, "detail_json": detail_json, "updated_at": updated_at}


class FakeRequest:
    def __init__(self, env, headers):
        self.scope = {"env": env}
        self.headers = headers


def signed_headers(secret: str) -> dict[str, str]:
    raw = json.dumps({"uid": "auto-user"}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def test_auto_model_pick_uses_workers_ai_and_reuses_d1_cache():
    secret = "test-secret"
    env = SimpleNamespace(
        INTERNAL_ASSERTION_SECRET=secret,
        APP_DB=FakeDb(),
        WORKERS_AI_CHAT_MODEL="@cf/meta/llama-3.2-3b-instruct",
    )
    request = FakeRequest(env, signed_headers(secret))

    first = asyncio.run(routes.auto_model_pick(request))
    assert first["provider"] == "workers-ai"
    assert first["detail"] == {
        "model": "@cf/meta/llama-3.2-3b-instruct",
        "reason": "workers-ai-native",
    }
    assert first["attribution"] == "https://developers.cloudflare.com/workers-ai/"

    second = asyncio.run(routes.auto_model_pick(request))
    assert second == first


def test_auto_model_pick_refreshes_legacy_cache_without_external_fetch():
    secret = "test-secret"
    database = FakeDb()
    database.row = {
        "provider": "geminiFlashLive",
        "detail_json": json.dumps({"reason": "legacy"}),
        "updated_at": time.time(),
    }
    env = SimpleNamespace(
        INTERNAL_ASSERTION_SECRET=secret,
        APP_DB=database,
        WORKERS_AI_CHAT_MODEL="@cf/meta/llama-3.2-3b-instruct",
    )
    request = FakeRequest(env, signed_headers(secret))

    result = asyncio.run(routes.auto_model_pick(request))

    assert result["provider"] == "workers-ai"
    assert result["detail"] == {
        "model": "@cf/meta/llama-3.2-3b-instruct",
        "reason": "workers-ai-native",
    }
    assert env.APP_DB.row["provider"] == "workers-ai"


def test_auto_model_pick_rejects_missing_auth():
    response = asyncio.run(
        routes.auto_model_pick(FakeRequest(SimpleNamespace(INTERNAL_ASSERTION_SECRET="secret"), {}))
    )
    assert response.status_code == 401
