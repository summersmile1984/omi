import asyncio
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from app_projection_routes import get_approved_apps, get_popular_apps  # noqa: E402


class FakeStatement:
    def __init__(self, rows):
        self.rows = rows

    def bind(self, *_args):
        return self

    async def all(self):
        return {"results": self.rows}


class FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def prepare(self, _sql):
        return FakeStatement(self.rows)


class FakeRequest:
    def __init__(self, env, headers=None, query=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.query_params = query or {}


def signed_headers(secret: str, uid: str = "catalog-user"):
    encoded = base64.urlsafe_b64encode(
        json.dumps({"uid": uid, "authority": "better-auth", "requestId": "catalog-test"}, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def catalog_row(app_id: str, *, popular: int = 0, installs: int = 0, disabled: int = 0, capabilities=None):
    payload = {
        "id": app_id,
        "name": app_id,
        "description": "catalog app",
        "capabilities": capabilities or ["chat"],
        "reviews": [{"score": 5}],
    }
    return {
        "id": app_id,
        "approved": 1,
        "disabled": disabled,
        "is_popular": popular,
        "installs": installs,
        "rating_avg": 4.5,
        "rating_count": 2,
        "data_json": json.dumps(payload),
    }


def test_approved_projection_filters_disabled_and_persona_apps_and_hides_reviews():
    secret = "catalog-secret"
    rows = [
        catalog_row("popular", popular=1, installs=20),
        catalog_row("disabled", disabled=1),
        catalog_row("persona", capabilities=["persona"]),
    ]
    env = type("Env", (), {"APP_DB": FakeDb(rows), "INTERNAL_ASSERTION_SECRET": secret})()

    result = asyncio.run(get_approved_apps(FakeRequest(env)))

    assert [app["id"] for app in result] == ["popular"]
    assert result[0]["is_popular"] is True
    assert "reviews" not in result[0]


def test_popular_projection_requires_signed_auth_and_can_include_reviews():
    secret = "catalog-secret"
    rows = [catalog_row("popular", popular=1, installs=20)]
    env = type("Env", (), {"APP_DB": FakeDb(rows), "INTERNAL_ASSERTION_SECRET": secret})()

    unauthorized = asyncio.run(get_popular_apps(FakeRequest(env)))
    result = asyncio.run(
        get_popular_apps(FakeRequest(env, signed_headers(secret), {"include_reviews": "true"}))
    )

    assert unauthorized.status_code == 401
    assert result[0]["reviews"] == [{"score": 5}]


def test_catalog_rejects_malformed_query_and_projection_rows():
    secret = "catalog-secret"
    env = type(
        "Env",
        (),
        {
            "APP_DB": FakeDb([{"id": "bad", "approved": 1, "disabled": 0, "data_json": "[]"}]),
            "INTERNAL_ASSERTION_SECRET": secret,
        },
    )()

    bad_query = asyncio.run(get_approved_apps(FakeRequest(env, query={"include_reviews": "maybe"})))
    malformed = asyncio.run(get_approved_apps(FakeRequest(env)))

    assert bad_query.status_code == 400
    assert malformed.status_code == 503
