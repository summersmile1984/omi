import asyncio
import base64
import hashlib
import hmac
import json
import sys
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


def test_auto_model_pick_defaults_and_reuses_d1_cache(monkeypatch):
    secret = "test-secret"
    env = SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, APP_DB=FakeDb())
    request = FakeRequest(env, signed_headers(secret))

    first = asyncio.run(routes.auto_model_pick(request))
    assert first["provider"] == "geminiFlashLive"
    assert first["detail"] == {"reason": "no ARTIFICIALANALYSIS_API_KEY; default to Gemini"}
    assert first["attribution"] == "https://artificialanalysis.ai/"

    async def unexpected_fetch(*args, **kwargs):
        raise AssertionError("fresh D1 cache should avoid upstream fetch")

    monkeypatch.setattr(routes, "worker_fetch", unexpected_fetch)
    second = asyncio.run(routes.auto_model_pick(request))
    assert second == first


def test_auto_model_pick_scores_provider_models_and_persists_result(monkeypatch):
    secret = "test-secret"
    env = SimpleNamespace(
        INTERNAL_ASSERTION_SECRET=secret,
        APP_DB=FakeDb(),
        ARTIFICIALANALYSIS_API_KEY="key",
        ARTIFICIALANALYSIS_API_URL="https://aa.example.test/models",
    )
    request = FakeRequest(env, signed_headers(secret))

    class FakeResponse:
        status = 200

        async def json(self):
            return {
                "data": [
                    {
                        "slug": "gemini-3-5-flash",
                        "evaluations": {"artificial_analysis_intelligence_index": 95},
                        "median_output_tokens_per_second": 200,
                    },
                    {
                        "slug": "gpt-5",
                        "evaluations": {"artificial_analysis_intelligence_index": 90},
                        "median_output_tokens_per_second": 250,
                    },
                ]
            }

    calls = {}

    async def fake_fetch(url, **options):
        calls["url"] = url
        calls["options"] = options
        return FakeResponse()

    monkeypatch.setattr(routes, "worker_fetch", fake_fetch)
    result = asyncio.run(routes.auto_model_pick(request))

    assert result["provider"] == "gptRealtime2"
    assert result["detail"]["scores"] == {"geminiFlashLive": 0.8975, "gptRealtime2": 0.935}
    assert calls["url"] == "https://aa.example.test/models"
    assert calls["options"]["headers"]["x-api-key"] == "key"
    assert env.APP_DB.row["provider"] == "gptRealtime2"


def test_auto_model_pick_rejects_missing_auth():
    response = asyncio.run(
        routes.auto_model_pick(FakeRequest(SimpleNamespace(INTERNAL_ASSERTION_SECRET="secret"), {}))
    )
    assert response.status_code == 401
