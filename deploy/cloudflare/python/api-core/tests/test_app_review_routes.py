import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from app_review_routes import (  # noqa: E402
    create_app_review,
    get_app_reviews,
    hydrate_app_reviews,
    reply_to_app_review,
    update_app_review,
)


class FakeStatement:
    def __init__(self, connection, sql):
        self.connection = connection
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def first(self):
        row = self.connection.execute(self.sql, self.args).fetchone()
        return dict(row) if row is not None else None

    async def all(self):
        rows = self.connection.execute(self.sql, self.args).fetchall()
        return {"results": [dict(row) for row in rows]}

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0035_app_catalog.sql").read_text())
        self.connection.executescript((migration_dir / "0045_app_reviews.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        results = []
        try:
            self.connection.execute("BEGIN")
            for statement in statements:
                cursor = self.connection.execute(statement.sql, statement.args)
                results.append({"meta": {"changes": cursor.rowcount}})
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return results


class FakeRequest:
    def __init__(self, env, headers=None, query=None, body=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.query_params = query or {}
        self.body = body

    async def json(self):
        return self.body


def signed_headers(secret: str, uid: str):
    raw = json.dumps({"uid": uid}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def insert_app(db: FakeDb, app_id: str, owner_uid: str | None = "owner"):
    db.connection.execute(
        "INSERT INTO cf_app_catalog "
        "(id, approved, status, disabled, is_popular, installs, rating_avg, rating_count, data_json, updated_at, owner_uid) "
        "VALUES (?, 1, 'approved', 0, 0, 0, NULL, 0, ?, 1, ?)",
        (app_id, json.dumps({"id": app_id, "name": "Reviewable App", "private": False}), owner_uid),
    )
    db.connection.commit()


def test_app_reviews_are_d1_owned_aggregated_and_hydrated():
    secret = "review-secret"
    db = FakeDb()
    insert_app(db, "app-1")
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    first = asyncio.run(
        create_app_review(
            FakeRequest(
                env,
                signed_headers(secret, "reviewer-1"),
                {"app_id": "app-1"},
                {"score": 7, "review": "Excellent", "username": "Alice"},
            )
        )
    )
    second = asyncio.run(
        create_app_review(
            FakeRequest(
                env,
                signed_headers(secret, "reviewer-2"),
                {"app_id": "app-1"},
                {"score": 3},
            )
        )
    )
    assert first == {"status": "ok"}
    assert second == {"status": "ok"}
    aggregate = db.connection.execute(
        "SELECT rating_avg, rating_count FROM cf_app_catalog WHERE id = 'app-1'"
    ).fetchone()
    assert dict(aggregate) == {"rating_avg": 4.0, "rating_count": 2}

    rated_at = db.connection.execute(
        "SELECT rated_at FROM cf_app_reviews WHERE app_id = 'app-1' AND reviewer_uid = 'reviewer-1'"
    ).fetchone()[0]
    updated = asyncio.run(
        update_app_review(
            FakeRequest(
                env,
                signed_headers(secret, "reviewer-1"),
                body={"score": 4, "review": "Still good"},
            ),
            "app-1",
        )
    )
    replied = asyncio.run(
        reply_to_app_review(
            FakeRequest(
                env,
                signed_headers(secret, "owner"),
                body={"reviewer_uid": "reviewer-1", "response": "Thanks"},
            ),
            "app-1",
        )
    )
    assert updated == {"status": "ok"}
    assert replied == {"status": "ok"}
    stored = db.connection.execute(
        "SELECT score, username, response, rated_at FROM cf_app_reviews "
        "WHERE app_id = 'app-1' AND reviewer_uid = 'reviewer-1'"
    ).fetchone()
    assert dict(stored) == {"score": 4.0, "username": "Alice", "response": "Thanks", "rated_at": rated_at}

    public_reviews = asyncio.run(get_app_reviews(FakeRequest(env), "app-1"))
    assert len(public_reviews) == 1
    assert public_reviews[0] == {
        "uid": "reviewer-1",
        "rated_at": public_reviews[0]["rated_at"],
        "score": 4.0,
        "review": "Still good",
        "username": "Alice",
        "response": "Thanks",
        "responded_at": public_reviews[0]["responded_at"],
    }
    assert public_reviews[0]["rated_at"].endswith("+00:00")
    assert public_reviews[0]["responded_at"].endswith("+00:00")

    apps = [{"id": "app-1", "rating_avg": 3.5, "rating_count": 2}]
    asyncio.run(hydrate_app_reviews(env, apps, current_uid="reviewer-2"))
    assert apps[0]["reviews"] == public_reviews
    assert apps[0]["user_review"]["uid"] == "reviewer-2"
    assert apps[0]["user_review"]["review"] == ""
    aggregate = db.connection.execute(
        "SELECT rating_avg, rating_count FROM cf_app_catalog WHERE id = 'app-1'"
    ).fetchone()
    assert dict(aggregate) == {"rating_avg": 3.5, "rating_count": 2}


def test_app_review_auth_owner_and_validation_boundaries_fail_closed():
    secret = "review-secret"
    db = FakeDb()
    insert_app(db, "app-1")
    insert_app(db, "ownerless", owner_uid=None)
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()
    body = {"score": 5, "review": "Good"}

    unauthorized = asyncio.run(create_app_review(FakeRequest(env, query={"app_id": "app-1"}, body=body)))
    self_review = asyncio.run(
        create_app_review(FakeRequest(env, signed_headers(secret, "owner"), {"app_id": "app-1"}, body))
    )
    missing_app = asyncio.run(
        create_app_review(FakeRequest(env, signed_headers(secret, "reviewer"), {"app_id": "missing"}, body))
    )
    ownerless = asyncio.run(
        create_app_review(FakeRequest(env, signed_headers(secret, "reviewer"), {"app_id": "ownerless"}, body))
    )
    missing_review = asyncio.run(
        update_app_review(
            FakeRequest(env, signed_headers(secret, "reviewer"), body={"score": 4}),
            "app-1",
        )
    )
    invalid_reply = asyncio.run(
        reply_to_app_review(
            FakeRequest(
                env,
                signed_headers(secret, "owner"),
                body={"reviewer_uid": "reviewer", "response": "   "},
            ),
            "app-1",
        )
    )
    non_owner_reply = asyncio.run(
        reply_to_app_review(
            FakeRequest(
                env,
                signed_headers(secret, "not-owner"),
                body={"reviewer_uid": "reviewer", "response": "Thanks"},
            ),
            "app-1",
        )
    )

    assert unauthorized.status_code == 401
    assert self_review.status_code == 403
    assert missing_app.status_code == 404
    assert ownerless.status_code == 503
    assert missing_review.status_code == 404
    assert invalid_reply.status_code == 422
    assert non_owner_reply.status_code == 403
    assert asyncio.run(get_app_reviews(FakeRequest(env), "missing")) == []
