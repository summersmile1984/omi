import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from csat_routes import router  # noqa: E402


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.fail = False
        for path in sorted((Path(__file__).parents[3] / "migrations/app").glob("*.sql")):
            self.connection.executescript(path.read_text())

    def prepare(self, sql):
        database = self

        class Statement:
            args = ()

            def bind(self, *args):
                self.args = args
                return self

            async def first(self):
                if database.fail:
                    raise RuntimeError("controlled store outage")
                row = database.connection.execute(sql, self.args).fetchone()
                database.connection.commit()
                return dict(row) if row else None

        return Statement()


@pytest.fixture
def target():
    database = Database()
    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def environment(request, call_next):
        request.scope["env"] = SimpleNamespace(
            APP_DB=database,
            INTERNAL_ASSERTION_SECRET="test-secret",
            BRAND_RUNTIME_JSON=json.dumps({"brand_id": "eddy", "display_name": "Eddy", "ai_persona_name": "Eddy"}),
        )
        return await call_next(request)

    def request(method, path, uid="owner", body=None):
        encoded = base64.urlsafe_b64encode(json.dumps({"uid": uid}).encode()).decode().rstrip("=")
        signature = hmac.new(b"test-secret", encoded.encode(), hashlib.sha256).digest()
        headers = (
            {
                "x-omi-auth-context": encoded,
                "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
            }
            if uid
            else {}
        )

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://core.test"
            ) as client:
                return await client.request(method, path, headers=headers, json=body)

        return asyncio.run(run())

    yield database, request
    database.connection.close()


def test_missing_config_serves_branded_defaults_and_normalizes_operator_copy(target):
    database, request = target
    assert request("GET", "/v1/csat/config", uid=None).status_code == 401
    default = request("GET", "/v1/csat/config").json()
    assert default == {
        "enabled": True,
        "title": "How would you rate Eddy Desktop?",
        "body": "",
        "thank_you_text": "Thank you!",
        "refer_cta_text": "Enjoying Eddy? Give a friend a free month.",
        "question_threshold": 3,
        "comment_max_score": 3,
        "revision": 0,
    }
    assert request("GET", "/v1/csat/config?platform=future-platform").json() == default
    raw = {
        "enabled": False,
        "title": "  Custom title  ",
        "body": 4,
        "thank_you_text": " ",
        "question_threshold": 99,
        "comment_max_score": -2,
        "revision": "6",
        "private": "never public",
    }
    database.connection.execute("INSERT INTO cf_csat_config VALUES ('product', ?)", (json.dumps(raw),))
    config = request("GET", "/v1/csat/config").json()
    assert config == {
        **default,
        "enabled": False,
        "title": "Custom title",
        "question_threshold": 50,
        "comment_max_score": 1,
        "revision": 6,
    }


def test_create_only_rating_preserves_first_answer_and_uses_server_comment_policy(target):
    database, request = target
    body = {
        "platform": "macos",
        "score": 2,
        "app_version": "  " + "v" * 60,
        "comment": " " + "c" * 700,
        "revision": 4,
        "uid": "other",
    }
    result = request("POST", "/v1/csat/ratings", body=body)
    assert result.status_code == 201 and result.json() == {"id": "macos_owner", "created": True}
    repeated = request("POST", "/v1/csat/ratings", body={**body, "score": 5})
    assert repeated.status_code == 409 and repeated.json() == {"id": "macos_owner", "created": False}
    assert request("POST", "/v1/csat/ratings", uid="other", body={**body, "score": 5}).status_code == 201
    assert request("POST", "/v1/csat/ratings", body={**body, "platform": "windows"}).status_code == 201
    rows = {row["id"]: dict(row) for row in database.connection.execute("SELECT * FROM cf_csat_ratings")}
    assert rows["macos_owner"]["score"] == 2
    assert rows["macos_owner"]["comment"] == "c" * 500
    assert rows["macos_owner"]["app_version"] == "v" * 32
    assert rows["macos_other"]["comment"] == ""
    with pytest.raises(sqlite3.IntegrityError, match="create-only"):
        database.connection.execute("UPDATE cf_csat_ratings SET score = 4 WHERE id = 'macos_owner'")


@pytest.mark.parametrize(
    "body,status",
    [
        ({}, 422),
        ({"platform": "macos", "score": "wrong"}, 422),
        ({"platform": "unknown", "score": 3}, 400),
        ({"platform": "macos", "score": 6}, 400),
        ({"platform": "macos", "score": 3, "revision": -1}, 400),
    ],
)
def test_invalid_ratings_never_create_state(target, body, status):
    database, request = target
    assert request("POST", "/v1/csat/ratings", body=body).status_code == status
    assert database.connection.execute("SELECT COUNT(*) FROM cf_csat_ratings").fetchone()[0] == 0


def test_outage_and_deletion_fence_cannot_be_reported_as_duplicate_success(target):
    database, request = target
    body = {"platform": "macos", "score": 3}
    assert request("POST", "/v1/csat/ratings", uid=None, body=body).status_code == 401
    database.fail = True
    assert request("GET", "/v1/csat/config").status_code == 503
    assert request("POST", "/v1/csat/ratings", body=body).status_code == 503
    database.fail = False
    assert request("POST", "/v1/csat/ratings", body=body).status_code == 201
    database.connection.execute(
        "INSERT INTO cf_account_deletion_tombstones (uid, completed_at, expires_at) VALUES ('owner', 1, 9999999999)"
    )
    for platform in ("macos", "ios"):
        assert request("POST", "/v1/csat/ratings", body={**body, "platform": platform}).status_code == 503
    assert database.connection.execute("SELECT COUNT(*) FROM cf_csat_ratings").fetchone()[0] == 1
