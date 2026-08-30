import asyncio
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from app_projection_routes import (  # noqa: E402
    check_is_tester,
    get_app,
    get_apps,
    get_approved_apps,
    get_persona_details,
    get_popular_apps,
)


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
        {
            **catalog_row("private-approved"),
            "data_json": json.dumps(
                {"id": "private-approved", "name": "private-approved", "capabilities": ["chat"], "private": True}
            ),
        },
    ]
    env = type("Env", (), {"APP_DB": FakeDb(rows), "INTERNAL_ASSERTION_SECRET": secret})()

    result = asyncio.run(get_approved_apps(FakeRequest(env)))

    assert [app["id"] for app in result] == ["popular"]
    assert result[0]["is_popular"] is True
    assert "reviews" not in result[0]


def test_owned_persona_projection_is_uid_scoped_and_hides_disabled_rows():
    secret = "catalog-secret"
    persona = {
        **catalog_row("persona", capabilities=["persona"]),
        "owner_uid": "catalog-user",
        "approved": 0,
        "disabled": 0,
        "data_json": json.dumps(
            {
                "id": "persona",
                "name": "My Persona",
                "capabilities": ["persona"],
                "persona_prompt": "private prompt",
            }
        ),
    }
    env = type("Env", (), {"APP_DB": FakeDb([persona]), "INTERNAL_ASSERTION_SECRET": secret})()

    unauthorized = asyncio.run(get_persona_details(FakeRequest(env)))
    result = asyncio.run(get_persona_details(FakeRequest(env, signed_headers(secret))))
    other = asyncio.run(get_persona_details(FakeRequest(env, signed_headers(secret, "other-user"))))

    assert unauthorized.status_code == 401
    assert result["id"] == "persona"
    assert result["persona_prompt"] == "private prompt"
    assert other.status_code == 404


def test_authenticated_catalog_unions_public_owned_and_assigned_apps_without_leaking_owner_fields():
    secret = "catalog-secret"
    public = {**catalog_row("public-app"), "owner_uid": "public-owner", "user_enabled": 1, "user_entitled": 0}
    owned = {
        **catalog_row("owned-app"),
        "owner_uid": "catalog-user",
        "approved": 0,
        "tester_access": 0,
        "user_enabled": 0,
        "user_entitled": 0,
    }
    assigned = {
        **catalog_row("assigned-app"),
        "owner_uid": "other-owner",
        "approved": 0,
        "tester_access": 1,
        "user_enabled": 0,
        "user_entitled": 0,
    }
    for row in (public, owned, assigned):
        payload = json.loads(row["data_json"])
        payload.update({"email": "owner@example.com", "chat_prompt": "owner-only", "private": row is not public})
        row["data_json"] = json.dumps(payload)
    env = type(
        "Env",
        (),
        {
            "APP_DB": FakeDb([public, owned, assigned], review_rows=[]),
            "INTERNAL_ASSERTION_SECRET": secret,
        },
    )()

    unauthorized = asyncio.run(get_apps(FakeRequest(env)))
    result = asyncio.run(get_apps(FakeRequest(env, signed_headers(secret), {"include_reviews": "false"})))

    assert unauthorized.status_code == 401
    assert [app["id"] for app in result] == ["public-app", "owned-app", "assigned-app"]
    assert result[0]["enabled"] is True
    assert result[1]["email"] == "owner@example.com"
    assert result[1]["chat_prompt"] == "owner-only"
    assert "email" not in result[2]
    assert "chat_prompt" not in result[2]
    assert result[2]["rejected"] is True


def test_tester_check_requires_auth_and_uses_durable_tester_membership():
    secret = "catalog-secret"
    member_env = type(
        "Env",
        (),
        {"APP_DB": FakeDb([], first_row={"uid": "catalog-user"}), "INTERNAL_ASSERTION_SECRET": secret},
    )()
    missing_env = type(
        "Env",
        (),
        {"APP_DB": FakeDb([], first_row=None), "INTERNAL_ASSERTION_SECRET": secret},
    )()

    assert asyncio.run(check_is_tester(FakeRequest(member_env))).status_code == 401
    assert asyncio.run(check_is_tester(FakeRequest(member_env, signed_headers(secret)))) == {"is_tester": True}
    assert asyncio.run(check_is_tester(FakeRequest(missing_env, signed_headers(secret)))) == {"is_tester": False}


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


def test_single_app_exposes_private_pending_disabled_record_only_to_its_owner():
    secret = "catalog-secret"
    row = {
        **catalog_row("owner-app", disabled=1),
        "owner_uid": "owner-user",
        "approved": 0,
        "user_enabled": 0,
    }
    payload = json.loads(row["data_json"])
    payload.update(
        {
            "private": True,
            "status": "under-review",
            "email": "owner@example.com",
            "chat_prompt": "owner-only prompt",
        }
    )
    row["data_json"] = json.dumps(payload)
    env = type(
        "Env",
        (),
        {"APP_DB": FakeDb([], first_row=row), "INTERNAL_ASSERTION_SECRET": secret},
    )()

    owner = asyncio.run(get_app(FakeRequest(env, signed_headers(secret, "owner-user")), "owner-app"))
    stranger = asyncio.run(get_app(FakeRequest(env, signed_headers(secret, "other-user")), "owner-app"))

    assert owner["id"] == "owner-app"
    assert owner["approved"] is False
    assert owner["disabled"] is True
    assert owner["private"] is True
    assert owner["email"] == "owner@example.com"
    assert owner["chat_prompt"] == "owner-only prompt"
    assert stranger.status_code == 404


def test_single_app_exposes_assigned_unapproved_private_record_to_tester_but_not_stranger():
    secret = "catalog-secret"
    row = {
        **catalog_row("tester-app"),
        "owner_uid": "owner-user",
        "approved": 0,
        "tester_access": 1,
        "user_enabled": 0,
        "user_entitled": 0,
    }
    payload = json.loads(row["data_json"])
    payload.update({"private": True, "email": "owner@example.com", "chat_prompt": "owner-only"})
    row["data_json"] = json.dumps(payload)
    tester_env = type(
        "Env",
        (),
        {"APP_DB": FakeDb([], first_row=row), "INTERNAL_ASSERTION_SECRET": secret},
    )()
    stranger_row = {**row, "tester_access": 0}
    stranger_env = type(
        "Env",
        (),
        {"APP_DB": FakeDb([], first_row=stranger_row), "INTERNAL_ASSERTION_SECRET": secret},
    )()

    tester = asyncio.run(get_app(FakeRequest(tester_env, signed_headers(secret, "tester-user")), "tester-app"))
    stranger = asyncio.run(get_app(FakeRequest(stranger_env, signed_headers(secret, "stranger")), "tester-app"))

    assert tester["id"] == "tester-app"
    assert "email" not in tester
    assert "chat_prompt" not in tester
    assert stranger.status_code == 404


def test_single_public_app_strips_owner_only_payload_fields():
    secret = "catalog-secret"
    row = {**catalog_row("public-app"), "owner_uid": "owner-user", "user_enabled": 0}
    payload = json.loads(row["data_json"])
    payload.update(
        {
            "email": "owner@example.com",
            "chat_prompt": "private implementation prompt",
            "payment_product_id": "prod_private",
            "external_integration": {"mcp_oauth_tokens": {"access_token": "secret"}},
        }
    )
    row["data_json"] = json.dumps(payload)
    env = type(
        "Env",
        (),
        {"APP_DB": FakeDb([], first_row=row), "INTERNAL_ASSERTION_SECRET": secret},
    )()

    result = asyncio.run(get_app(FakeRequest(env, signed_headers(secret, "viewer-user")), "public-app"))

    assert result["id"] == "public-app"
    assert "email" not in result
    assert "chat_prompt" not in result
    assert "payment_product_id" not in result
    assert result["external_integration"] == {}
