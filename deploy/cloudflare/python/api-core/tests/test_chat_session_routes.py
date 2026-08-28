import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from chat_session_routes import (  # noqa: E402
    create_chat_session,
    delete_chat_session,
    delete_desktop_messages,
    get_chat_session,
    get_desktop_messages,
    list_chat_sessions,
    rate_desktop_message,
    reconcile_desktop_messages,
    save_desktop_message,
    update_chat_session,
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
        self.fail_next_batch = False
        self.before_batch = None
        self.connection.executescript(
            "CREATE TABLE cf_account_deletion_intents (uid TEXT PRIMARY KEY);"
            "CREATE TABLE cf_account_deletion_tombstones (uid TEXT PRIMARY KEY);"
        )
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for migration in ("0042_chat_messages.sql", "0053_user_feedback.sql", "0054_chat_sessions.sql"):
            self.connection.executescript((migration_dir / migration).read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        if self.fail_next_batch:
            self.fail_next_batch = False
            raise RuntimeError("simulated batch failure")
        if self.before_batch is not None:
            callback = self.before_batch
            self.before_batch = None
            callback()
        results = []
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                cursor = self.connection.execute(statement.sql, statement.args)
                results.append({"meta": {"changes": cursor.rowcount}})
            self.connection.commit()
            return results
        except Exception:
            self.connection.rollback()
            raise


class FakeRequest:
    def __init__(self, env, headers=None, query=None, body=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.query_params = query or {}
        self.body = body

    async def json(self):
        return self.body


def signed_headers(secret: str, uid: str = "chat-user"):
    raw = json.dumps({"uid": uid}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def environment(db, secret="chat-session-secret"):
    return type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()


def response_json(response):
    return json.loads(response.body)


def insert_session(db, session_id, *, uid="chat-user", app_id=None, message_count=0, preview=None):
    db.connection.execute(
        "INSERT INTO cf_chat_sessions "
        "(uid, id, title, preview, created_at, updated_at, app_id, message_count, starred) "
        "VALUES (?, ?, 'New Chat', ?, 1, 1, ?, ?, 0)",
        (uid, session_id, preview, app_id, message_count),
    )
    db.connection.commit()


def insert_message(
    db,
    message_id,
    *,
    uid="chat-user",
    app_id=None,
    session_id="session-1",
    created_at=1,
    reported=False,
):
    message = {
        "id": message_id,
        "text": message_id,
        "sender": "ai",
        "type": "text",
        "created_at": "2026-08-29T00:00:00+00:00",
        "app_id": app_id,
        "plugin_id": app_id,
        "chat_session_id": session_id,
        "session_id": session_id,
        "reported": reported,
        "rating": None,
    }
    db.connection.execute(
        "INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) VALUES (?, ?, ?, ?, ?)",
        (uid, message_id, app_id, created_at, json.dumps(message)),
    )
    db.connection.commit()


def test_chat_session_crud_is_uid_and_app_scoped():
    secret = "chat-session-secret"
    db = FakeDb()
    env = environment(db, secret)
    created = asyncio.run(
        create_chat_session(FakeRequest(env, signed_headers(secret), body={"title": "Research", "app_id": "assistant"}))
    )
    assert created["title"] == "Research"
    assert created["app_id"] == "assistant"
    session_id = created["id"]

    listed = asyncio.run(list_chat_sessions(FakeRequest(env, signed_headers(secret), query={"app_id": "assistant"})))
    assert [row["id"] for row in listed] == [session_id]
    hidden = asyncio.run(get_chat_session(FakeRequest(env, signed_headers(secret, "other-user")), session_id))
    assert hidden.status_code == 404

    updated = asyncio.run(
        update_chat_session(
            FakeRequest(env, signed_headers(secret), body={"title": "Updated", "starred": True}),
            session_id,
        )
    )
    assert updated["title"] == "Updated"
    assert updated["starred"] is True
    starred = asyncio.run(
        list_chat_sessions(FakeRequest(env, signed_headers(secret), query={"app_id": "assistant", "starred": "true"}))
    )
    assert len(starred) == 1

    deleted = asyncio.run(delete_chat_session(FakeRequest(env, signed_headers(secret)), session_id))
    assert deleted == {"status": "ok"}
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_sessions").fetchone()[0] == 0


def test_desktop_save_is_idempotent_and_accepts_monotonic_revisions():
    secret = "chat-session-secret"
    db = FakeDb()
    env = environment(db, secret)
    request = {
        "text": "Draft",
        "sender": "human",
        "client_message_id": "client-1",
        "journal_revision": 1,
        "content_blocks": [{"type": "text", "text": "Draft"}],
    }
    created = asyncio.run(save_desktop_message(FakeRequest(env, signed_headers(secret), body=request)))
    assert created["created"] is True
    assert created["id"] == "client-1"
    session_id = created["session_id"]

    repeated = asyncio.run(save_desktop_message(FakeRequest(env, signed_headers(secret), body=request)))
    assert repeated["created"] is False
    assert repeated["updated"] is False
    conflict = asyncio.run(
        save_desktop_message(FakeRequest(env, signed_headers(secret), body={**request, "text": "Conflict"}))
    )
    assert conflict.status_code == 409

    revised = asyncio.run(
        save_desktop_message(
            FakeRequest(
                env,
                signed_headers(secret),
                body={
                    **request,
                    "text": "Final",
                    "content_blocks": [{"type": "text", "text": "Final"}],
                    "journal_revision": 2,
                },
            )
        )
    )
    assert revised["created"] is False
    assert revised["updated"] is True
    assert revised["journal_revision"] == 2

    stale = asyncio.run(
        save_desktop_message(
            FakeRequest(
                env,
                signed_headers(secret),
                body={
                    **request,
                    "text": "Stale",
                    "content_blocks": [{"type": "text", "text": "Stale"}],
                    "journal_revision": 1,
                },
            )
        )
    )
    assert stale["created"] is False
    assert stale["updated"] is False
    assert stale["journal_revision"] == 2
    message = json.loads(
        db.connection.execute("SELECT message_json FROM cf_chat_messages WHERE id = 'client-1'").fetchone()[0]
    )
    assert message["text"] == "Final"
    session = db.connection.execute(
        "SELECT message_count, preview FROM cf_chat_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    assert dict(session) == {"message_count": 1, "preview": "Draft"}
    quota_event = db.connection.execute(
        "SELECT source, message_id, chat_session_id FROM cf_chat_quota_events WHERE uid = 'chat-user'"
    ).fetchone()
    assert dict(quota_event) == {
        "source": "desktop_messages",
        "message_id": "client-1",
        "chat_session_id": session_id,
    }
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_quota_events").fetchone()[0] == 1


def test_desktop_lists_reconciles_and_deletes_with_session_metadata():
    secret = "chat-session-secret"
    db = FakeDb()
    env = environment(db, secret)
    insert_session(db, "session-1", message_count=3, preview="m3")
    insert_message(db, "m1", created_at=1)
    insert_message(db, "m2", created_at=2, reported=True)
    insert_message(db, "m3", created_at=3)
    insert_session(db, "session-2", uid="other-user", message_count=1)
    insert_message(db, "other", uid="other-user", session_id="session-2", created_at=4)

    listed = asyncio.run(
        get_desktop_messages(FakeRequest(env, signed_headers(secret), query={"session_id": "session-1"}))
    )
    assert [message["id"] for message in listed] == ["m3", "m1"]
    first_page = asyncio.run(
        reconcile_desktop_messages(
            FakeRequest(env, signed_headers(secret), query={"session_id": "session-1", "limit": "1"})
        )
    )
    assert [message["id"] for message in first_page["messages"]] == ["m3"]
    second_page = asyncio.run(
        reconcile_desktop_messages(
            FakeRequest(
                env,
                signed_headers(secret),
                query={"session_id": "session-1", "limit": "2", "cursor": first_page["next_cursor"]},
            )
        )
    )
    assert [message["id"] for message in second_page["messages"]] == ["m1"]

    def concurrent_message_write():
        insert_message(db, "m4", created_at=4)
        db.connection.execute("UPDATE cf_chat_sessions SET message_count = 4, preview = 'm4' WHERE id = 'session-1'")
        db.connection.commit()

    db.before_batch = concurrent_message_write

    deleted = asyncio.run(
        delete_desktop_messages(FakeRequest(env, signed_headers(secret), query={"session_id": "session-1"}))
    )
    assert deleted == {"status": "ok", "deleted_count": 4}
    session = db.connection.execute(
        "SELECT message_count, preview FROM cf_chat_sessions WHERE id = 'session-1'"
    ).fetchone()
    assert dict(session) == {"message_count": 0, "preview": None}
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_messages").fetchone()[0] == 1


def test_desktop_rating_requires_owned_message_and_updates_feedback_atomically():
    secret = "chat-session-secret"
    db = FakeDb()
    env = environment(db, secret)
    insert_session(db, "session-1", message_count=1)
    insert_message(db, "m1")

    rated = asyncio.run(rate_desktop_message(FakeRequest(env, signed_headers(secret), body={"rating": -1}), "m1"))
    assert rated == {"status": "ok"}
    message = json.loads(
        db.connection.execute("SELECT message_json FROM cf_chat_messages WHERE id = 'm1'").fetchone()[0]
    )
    assert message["rating"] == -1
    feedback = db.connection.execute(
        "SELECT value FROM cf_user_feedback WHERE uid = 'chat-user' AND subject_id = 'm1'"
    ).fetchone()
    assert feedback[0] == -1

    missing = asyncio.run(rate_desktop_message(FakeRequest(env, signed_headers(secret), body={"rating": 1}), "missing"))
    assert missing.status_code == 404
    zero = asyncio.run(rate_desktop_message(FakeRequest(env, signed_headers(secret), body={"rating": 0}), "m1"))
    assert zero.status_code == 400


def test_chat_session_routes_reject_bad_auth_and_fail_closed_on_d1_errors():
    secret = "chat-session-secret"
    db = FakeDb()
    env = environment(db, secret)
    unauthorized = asyncio.run(list_chat_sessions(FakeRequest(env)))
    assert unauthorized.status_code == 401
    invalid = asyncio.run(
        save_desktop_message(FakeRequest(env, signed_headers(secret), body={"text": "", "sender": "human"}))
    )
    assert invalid.status_code == 400

    insert_session(db, "session-1", message_count=1)
    insert_message(db, "m1")
    db.fail_next_batch = True
    failed = asyncio.run(
        delete_desktop_messages(FakeRequest(env, signed_headers(secret), query={"session_id": "session-1"}))
    )
    assert failed.status_code == 503
    assert response_json(failed) == {"error": "messages unavailable"}
    assert db.connection.execute("SELECT COUNT(*) FROM cf_chat_messages").fetchone()[0] == 1
