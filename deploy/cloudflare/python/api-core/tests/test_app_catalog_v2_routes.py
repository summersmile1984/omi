import asyncio
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from app_catalog_v2_routes import get_apps_v2, get_capability_apps_grouped, search_apps  # noqa: E402


class FakeStatement:
    def __init__(self, rows):
        self.rows = rows

    def bind(self, *_args):
        return self

    async def all(self):
        return {"results": self.rows}


class FakeDb:
    def __init__(self, rows, enabled_rows=None):
        self.rows = rows
        self.enabled_rows = enabled_rows or []

    def prepare(self, sql):
        return FakeStatement(self.enabled_rows if "cf_user_enabled_apps" in sql else self.rows)


class FakeRequest:
    def __init__(self, env, query=None, headers=None):
        self.scope = {"env": env}
        self.query_params = query or {}
        self.headers = headers or {}


def signed_headers(secret: str, uid: str = "install-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "catalog-search-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def row(app_id, *, capabilities, popular=0, installs=0, category="other", external=None):
    payload = {
        "id": app_id,
        "name": app_id,
        "description": "public catalog app",
        "category": category,
        "author": "Omi",
        "capabilities": capabilities,
        "external_integration": external,
        "is_paid": False,
    }
    return {
        "id": app_id,
        "approved": 1,
        "disabled": 0,
        "is_popular": popular,
        "installs": installs,
        "rating_avg": 4.5,
        "rating_count": 2,
        "data_json": json.dumps(payload),
    }


def test_v2_catalog_supports_capability_category_and_grouped_reads():
    env = type(
        "Env",
        (),
        {
            "APP_DB": FakeDb(
                [
                    row("featured", capabilities=["chat"], popular=1, installs=20),
                    row("chat-app", capabilities=["chat"], installs=3, category="productivity-and-organization"),
                    row("memory-app", capabilities=["memories"], installs=2),
                    row("persona", capabilities=["persona"], installs=100),
                ]
            )
        },
    )()

    capability = asyncio.run(get_apps_v2(FakeRequest(env, {"capability": "chat"})))
    assert [item["id"] for item in capability["data"]] == ["featured", "chat-app"]
    assert capability["pagination"]["total"] == 2

    category = asyncio.run(get_apps_v2(FakeRequest(env, {"category": "productivity-and-organization"})))
    assert [item["id"] for item in category["data"]] == ["chat-app"]

    grouped = asyncio.run(get_apps_v2(FakeRequest(env)))
    assert [group["capability"]["id"] for group in grouped["groups"]] == ["popular", "chat", "memories"]

    grouped_chat = asyncio.run(get_capability_apps_grouped(FakeRequest(env), "chat"))
    assert grouped_chat["meta"]["totalApps"] == 2
    assert grouped_chat["groups"][0]["category"]["id"] == "productivity-lifestyle"


def test_v2_catalog_excludes_private_and_sanitizes_external_auth_steps():
    env = type(
        "Env",
        (),
        {
            "APP_DB": FakeDb(
                [
                    row(
                        "integration",
                        capabilities=["external_integration"],
                        external={
                            "auth_steps": [{"name": "secret", "url": "https://secret.test"}],
                            "webhook_url": "https://hooks.test",
                        },
                    ),
                    {
                        **row("private", capabilities=["chat"]),
                        "data_json": json.dumps({"id": "private", "capabilities": ["chat"], "private": True}),
                    },
                ]
            )
        },
    )()
    result = asyncio.run(get_apps_v2(FakeRequest(env, {"capability": "external_integration"})))
    assert result["data"][0]["id"] == "integration"
    assert result["data"][0]["external_integration"] == {"webhook_url": "https://hooks.test"}


def test_v2_catalog_search_supports_text_filters_sorting_and_installed_state():
    secret = "catalog-search-secret"
    env = type(
        "Env",
        (),
        {
            "APP_DB": FakeDb(
                [
                    row("chat-app", capabilities=["chat"], installs=3),
                    row("memory-app", capabilities=["memories"], installs=2),
                ],
                [{"app_id": "chat-app"}],
            ),
            "INTERNAL_ASSERTION_SECRET": secret,
        },
    )()
    query = {"q": "chat", "sort": "installs_desc"}
    result = asyncio.run(search_apps(FakeRequest(env, query, signed_headers(secret))))
    assert [item["id"] for item in result["data"]] == ["chat-app"]
    assert result["filters"]["sort"] == "installs_desc"

    installed = asyncio.run(search_apps(FakeRequest(env, {"installed_apps": "true"}, signed_headers(secret))))
    assert [item["id"] for item in installed["data"]] == ["chat-app"]
    assert installed["data"][0]["enabled"] is True


def test_v2_catalog_rejects_invalid_query_and_malformed_rows():
    env = type("Env", (), {"APP_DB": FakeDb([])})()
    invalid = asyncio.run(get_apps_v2(FakeRequest(env, {"limit": "0"})))
    assert invalid.status_code == 400

    malformed_env = type(
        "Env",
        (),
        {"APP_DB": FakeDb([{"id": "bad", "approved": 1, "disabled": 0, "data_json": "[]"}])},
    )()
    malformed = asyncio.run(get_apps_v2(FakeRequest(malformed_env)))
    assert malformed.status_code == 503

    unauthorized = asyncio.run(search_apps(FakeRequest(env)))
    assert unauthorized.status_code == 401
