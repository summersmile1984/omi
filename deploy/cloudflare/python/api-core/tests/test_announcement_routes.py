import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from announcement_routes import (  # noqa: E402
    _compare_versions,
    dismiss_announcement,
    get_changelogs,
    get_features,
    get_general_announcements,
    get_pending_announcements,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration = Path(__file__).parents[3] / "migrations/app/0028_announcements.sql"
        self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


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
        self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": 1}}


class FakeRequest:
    def __init__(self, env, headers, body=None):
        self.scope = {"env": env}
        self.headers = headers
        self.body = body
        self.query_params = {}

    async def json(self):
        return self.body


def signed_headers(secret: str, uid: str = "announcement-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "announcement-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def seed(db: FakeDb, *, announcement_id: str, announcement_type: str, created_at: int, **values):
    if "targeting" in values:
        values["targeting_json"] = values.pop("targeting")
    if "display" in values:
        values["display_json"] = values.pop("display")
    columns = {
        "id": announcement_id,
        "type": announcement_type,
        "created_at": created_at,
        "active": 1,
        "device_models_json": "[]",
        "content_json": json.dumps({"title": announcement_id}),
        **values,
    }
    columns["device_models_json"] = json.dumps(columns["device_models_json"])
    for key in ("targeting_json", "display_json", "content_json"):
        if isinstance(columns.get(key), dict):
            columns[key] = json.dumps(columns[key])
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    db.connection.execute(f"INSERT INTO cf_announcements ({names}) VALUES ({placeholders})", tuple(columns.values()))
    db.connection.commit()


def test_announcement_reads_preserve_release_filters_and_version_ordering():
    db = FakeDb()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": "announcement-secret"})()
    seed(
        db,
        announcement_id="changelog-2",
        announcement_type="changelog",
        created_at=20,
        app_version="1.0.10+2",
    )
    seed(
        db,
        announcement_id="changelog-1",
        announcement_type="changelog",
        created_at=30,
        app_version="1.0.2",
    )
    seed(
        db,
        announcement_id="feature-ios",
        announcement_type="feature",
        created_at=40,
        app_version="1.0.10",
        device_models_json=["Omi Pro"],
    )
    seed(
        db,
        announcement_id="general-new",
        announcement_type="announcement",
        created_at=50,
    )
    seed(
        db,
        announcement_id="general-old",
        announcement_type="announcement",
        created_at=10,
    )

    changelogs = asyncio.run(get_changelogs(FakeRequest(env, {}), max_version="1.0.10", limit=5))
    assert [item["id"] for item in changelogs] == ["changelog-2", "changelog-1"]
    features = asyncio.run(
        get_features(FakeRequest(env, {}), version="1.0.10", version_type="app", device_model="Omi Pro")
    )
    assert [item["id"] for item in features] == ["feature-ios"]
    general = asyncio.run(get_general_announcements(FakeRequest(env, {}), last_checked_at="1970-01-01T00:00:20Z"))
    assert [item["id"] for item in general] == ["general-new"]
    assert _compare_versions("v1.0.10+2", "1.0.10+1") > 0
    assert _compare_versions("1.0.10", "1.0.10+99") == 0


def test_pending_announcement_projection_is_uid_scoped_and_dismissible():
    db = FakeDb()
    secret = "announcement-secret"
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()
    seed(
        db,
        announcement_id="pending-immediate",
        announcement_type="announcement",
        created_at=100,
        targeting={"trigger": "immediate", "platforms": ["ios"], "app_version_min": "1.0.0"},
        display={"priority": 3, "show_once": True},
    )
    seed(
        db,
        announcement_id="pending-android",
        announcement_type="announcement",
        created_at=101,
        targeting={"trigger": "version_upgrade", "platforms": ["android"]},
        display={"priority": 2},
    )

    unauthenticated = asyncio.run(
        get_pending_announcements(FakeRequest(env, {}), app_version="1.0.2", platform="ios", trigger="app_launch")
    )
    assert unauthenticated.status_code == 401
    pending = asyncio.run(
        get_pending_announcements(
            FakeRequest(env, signed_headers(secret)), app_version="1.0.2", platform="ios", trigger="app_launch"
        )
    )
    assert [item["id"] for item in pending] == ["pending-immediate"]

    dismissed = asyncio.run(
        dismiss_announcement(FakeRequest(env, signed_headers(secret), {"cta_clicked": True}), "pending-immediate")
    )
    assert dismissed == {"success": True, "message": "Announcement dismissed"}
    after = asyncio.run(
        get_pending_announcements(
            FakeRequest(env, signed_headers(secret)), app_version="1.0.2", platform="ios", trigger="app_launch"
        )
    )
    assert after == []
    invalid_platform = asyncio.run(
        get_pending_announcements(
            FakeRequest(env, signed_headers(secret)), app_version="1.0.2", platform="web", trigger="app_launch"
        )
    )
    assert invalid_platform.status_code == 400

    other_user = asyncio.run(
        get_pending_announcements(
            FakeRequest(env, signed_headers(secret, uid="other-user")),
            app_version="1.0.2",
            platform="ios",
            trigger="app_launch",
        )
    )
    assert [item["id"] for item in other_user] == ["pending-immediate"]
