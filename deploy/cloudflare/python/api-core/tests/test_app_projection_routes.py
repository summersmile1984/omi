import asyncio
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from app_projection_routes import get_app, get_approved_apps, get_popular_apps  # noqa: E402


class FakeStatement:
    def __init__(self, rows, first_row=None):
        self.rows = rows
        self.first_row = first_row

    def bind(self, *_args):
        return self

    async def all(self):
        return {"results": self.rows}

    async def first(self):
        return self.first_row


class FakeDb:
    def __init__(self, rows, first_row=None, review_rows=None):
        self.rows = rows
        self.first_row = first_row
        self.review_rows = review_rows or []

    def prepare(self, sql):
        if "FROM cf_app_reviews" in sql:
            return FakeStatement(self.review_rows)
        return FakeStatement(self.rows, self.first_row)


class FakeRequest:
    def __init__(self, env, headers=None, query=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.query_params = query or {}


def signed_headers(secret: str, uid: str = "catalog-user"):
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"uid": uid, "authority": "better-auth", "requestId": "catalog-test"}, separators=(",", ":")
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
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


def review_row(app_id: str, uid: str = "catalog-user"):
    return {
        "app_id": app_id,
        "reviewer_uid": uid,
        "score": 5,
        "review_text": "Excellent",
        "username": "Alice",
        "response": "Thanks",
        "rated_at": 1,
        "responded_at": 2,
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
    env = type(
        "Env",
        (),
        {"APP_DB": FakeDb(rows, review_rows=[review_row("popular")]), "INTERNAL_ASSERTION_SECRET": secret},
    )()

    unauthorized = asyncio.run(get_popular_apps(FakeRequest(env)))
    result = asyncio.run(get_popular_apps(FakeRequest(env, signed_headers(secret), {"include_reviews": "true"})))

    assert unauthorized.status_code == 401
    assert result[0]["reviews"] == [
        {
            "uid": "catalog-user",
            "rated_at": "1970-01-01T00:00:01+00:00",
            "score": 5.0,
            "review": "Excellent",
            "username": "Alice",
            "response": "Thanks",
            "responded_at": "1970-01-01T00:00:02+00:00",
        }
    ]
    assert result[0]["user_review"] == result[0]["reviews"][0]


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


def test_single_app_requires_auth_and_returns_user_install_state_without_private_fields():
    secret = "catalog-secret"
    row = {
        **catalog_row("summary-app", installs=7),
        "user_enabled": 1,
    }
    env = type(
        "Env",
        (),
        {"APP_DB": FakeDb([], first_row=row), "INTERNAL_ASSERTION_SECRET": secret},
    )()

    unauthorized = asyncio.run(get_app(FakeRequest(env), "summary-app"))
    result = asyncio.run(get_app(FakeRequest(env, signed_headers(secret)), "summary-app"))

    assert unauthorized.status_code == 401
    assert result["id"] == "summary-app"
    assert result["enabled"] is True
    assert result["installs"] == 7
    assert result["reviews"] == []
    assert result["user_review"] is None


def test_single_paid_app_exposes_user_bound_payment_link_and_entitlement_state():
    secret = "catalog-secret"
    payload_row = catalog_row("paid-app", installs=3)
    payload = json.loads(payload_row["data_json"])
    payload.update(
        {
            "is_paid": True,
            "payment_link": "https://buy.stripe.com/test_link",
            "payment_link_id": "plink_test",
        }
    )
    row = {
        **payload_row,
        "data_json": json.dumps(payload),
        "user_enabled": 1,
        "user_entitled": 1,
    }
    env = type(
        "Env",
        (),
        {"APP_DB": FakeDb([], first_row=row), "INTERNAL_ASSERTION_SECRET": secret},
    )()

    entitled = asyncio.run(get_app(FakeRequest(env, signed_headers(secret, "paid-user")), "paid-app"))

    assert entitled["is_user_paid"] is True
    assert entitled["enabled"] is True
    assert entitled["payment_link"] == ("https://buy.stripe.com/test_link?client_reference_id=uid_paid-user")

    row["user_entitled"] = 0
    expired = asyncio.run(get_app(FakeRequest(env, signed_headers(secret, "paid-user")), "paid-app"))
    assert expired["is_user_paid"] is False
    assert expired["enabled"] is False


def test_single_app_hides_unavailable_rows_and_fails_closed_for_malformed_projection():
    secret = "catalog-secret"
    headers = signed_headers(secret)
    wrong_row = {**catalog_row("other-app"), "user_enabled": 0}
    wrong_env = type(
        "Env",
        (),
        {"APP_DB": FakeDb([], first_row=wrong_row), "INTERNAL_ASSERTION_SECRET": secret},
    )()
    malformed_row = {**catalog_row("summary-app"), "data_json": "[]", "user_enabled": 0}
    malformed_env = type(
        "Env",
        (),
        {"APP_DB": FakeDb([], first_row=malformed_row), "INTERNAL_ASSERTION_SECRET": secret},
    )()

    missing = asyncio.run(get_app(FakeRequest(wrong_env, headers), "summary-app"))
    malformed = asyncio.run(get_app(FakeRequest(malformed_env, headers), "summary-app"))

    assert missing.status_code == 404
    assert malformed.status_code == 503
