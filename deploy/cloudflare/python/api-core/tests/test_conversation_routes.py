import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from conversation_routes import (  # noqa: E402
    count_conversations,
    get_conversation,
    get_conversation_analytics,
    get_conversation_photos,
    get_conversation_transcripts,
    conversation_has_recording,
    list_conversations,
    patch_conversation_action_items,
    patch_conversation_events,
    patch_conversation_segment_text,
    patch_conversation_title,
    set_conversation_starred,
    store_conversation_projection,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0017_people.sql").read_text())
        self.connection.executescript((migration_dir / "0016_action_items.sql").read_text())
        self.connection.executescript((migration_dir / "0032_conversations.sql").read_text())
        self.connection.executescript((migration_dir / "0033_conversation_sync_flag.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        for statement in statements:
            self.connection.execute(statement.sql, statement.args)
        self.connection.commit()


class FakeBucket:
    def __init__(self):
        self.keys: set[str] = set()

    async def head(self, key):
        return {"key": key} if key in self.keys else None


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


class FakeRequest:
    def __init__(self, env, headers, query=None, body=None):
        self.scope = {"env": env}
        self.headers = headers
        self.query_params = query or {}
        self._body = body

    async def body(self):
        return json.dumps(self._body).encode()


def signed_headers(secret: str, uid: str = "conversation-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "conversation-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def insert_conversation(
    db: FakeDb,
    *,
    uid: str,
    conversation_id: str,
    created_at: int,
    locked: int = 0,
    photos: list[dict[str, object]] | None = None,
    transcript_segments: list[dict[str, object]] | None = None,
):
    db.connection.execute(
        "INSERT INTO cf_conversations "
        "(uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
        "starred, discarded, is_locked, deferred, folder_id, structured_json, transcript_segments_json, photos_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uid,
            conversation_id,
            created_at,
            created_at,
            created_at,
            created_at + 60,
            "omi",
            "en",
            "completed",
            "private",
            1,
            0,
            locked,
            0,
            "folder-1",
            json.dumps({"title": conversation_id, "overview": "overview", "category": "work", "action_items": [{"description": "task"}], "events": [{"title": "event"}]}),
            json.dumps(
                transcript_segments
                if transcript_segments is not None
                else [{"id": "segment-1", "text": "hello", "start": 0, "end": 1, "is_user": True}]
            ),
            json.dumps(photos or []),
        ),
    )
    db.connection.commit()


def test_conversation_projection_lists_filters_and_redacts_list_details():
    secret = "conversation-secret"
    db = FakeDb()
    insert_conversation(db, uid="conversation-user", conversation_id="new", created_at=200)
    insert_conversation(db, uid="conversation-user", conversation_id="locked", created_at=100, locked=1)
    insert_conversation(db, uid="other-user", conversation_id="other", created_at=300)
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    listed = asyncio.run(
        list_conversations(
            FakeRequest(
                env,
                signed_headers(secret),
                {"limit": "10", "starred": "true", "folder_id": "folder-1"},
            )
        )
    )
    assert [item["id"] for item in listed] == ["new", "locked"]
    assert listed[0]["transcript_segments"] == []
    assert listed[0]["structured"]["title"] == "new"
    assert listed[1]["structured"]["action_items"] == []

    filtered = asyncio.run(
        list_conversations(FakeRequest(env, signed_headers(secret), {"include_discarded": "false", "sources": "friend"}))
    )
    assert filtered == []


def test_conversation_projection_detail_count_uid_isolation_and_validation():
    secret = "conversation-secret"
    db = FakeDb()
    insert_conversation(db, uid="conversation-user", conversation_id="conv-1", created_at=200)
    insert_conversation(db, uid="other-user", conversation_id="conv-1", created_at=300)
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    detail = asyncio.run(get_conversation(FakeRequest(env, signed_headers(secret)), "conv-1"))
    assert detail["id"] == "conv-1"
    assert detail["transcript_segments"][0]["text"] == "hello"
    assert detail["structured"]["action_items"] == [{"description": "task"}]

    count = asyncio.run(count_conversations(FakeRequest(env, signed_headers(secret))))
    assert count == {"count": 1}
    missing = asyncio.run(get_conversation(FakeRequest(env, signed_headers(secret)), "missing"))
    assert missing.status_code == 404
    invalid = asyncio.run(list_conversations(FakeRequest(env, signed_headers(secret), {"limit": "0"})))
    assert invalid.status_code == 400
    unauthorized = asyncio.run(count_conversations(FakeRequest(env, {})))
    assert unauthorized.status_code == 401


def test_canonical_conversation_photos_are_uid_scoped_and_locked_rows_fail_closed():
    secret = "conversation-secret"
    db = FakeDb()
    photos = [{"id": "photo-1", "base64": "abc", "description": "cat", "discarded": False}]
    insert_conversation(db, uid="conversation-user", conversation_id="with-photos", created_at=200, photos=photos)
    insert_conversation(
        db,
        uid="conversation-user",
        conversation_id="locked-photos",
        created_at=100,
        locked=1,
        photos=photos,
    )
    insert_conversation(db, uid="other-user", conversation_id="with-photos", created_at=300, photos=photos)
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    result = asyncio.run(get_conversation_photos(FakeRequest(env, signed_headers(secret)), "with-photos"))
    assert result == photos
    assert asyncio.run(get_conversation_photos(FakeRequest(env, signed_headers(secret)), "locked-photos")) == []
    missing = asyncio.run(get_conversation_photos(FakeRequest(env, signed_headers(secret)), "missing"))
    assert missing.status_code == 404
    invalid = asyncio.run(get_conversation_photos(FakeRequest(env, signed_headers(secret)), "x" * 257))
    assert invalid.status_code == 400
    unauthorized = asyncio.run(get_conversation_photos(FakeRequest(env, {}), "with-photos"))
    assert unauthorized.status_code == 401


def test_canonical_segment_text_update_is_uid_scoped_locked_and_compare_and_set():
    secret = "conversation-secret"
    db = FakeDb()
    insert_conversation(db, uid="conversation-user", conversation_id="editable", created_at=200)
    insert_conversation(db, uid="conversation-user", conversation_id="locked", created_at=100, locked=1)
    insert_conversation(db, uid="other-user", conversation_id="editable", created_at=300)
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    updated = asyncio.run(
        patch_conversation_segment_text(
            FakeRequest(
                env,
                signed_headers(secret),
                body={"segment_id": "segment-1", "text": "edited"},
            ),
            "editable",
        )
    )
    assert updated == {"status": "Ok"}
    detail = asyncio.run(get_conversation(FakeRequest(env, signed_headers(secret)), "editable"))
    assert detail["transcript_segments"][0]["text"] == "edited"

    locked = asyncio.run(
        patch_conversation_segment_text(
            FakeRequest(env, signed_headers(secret), body={"segment_id": "segment-1", "text": "nope"}),
            "locked",
        )
    )
    assert locked.status_code == 402
    missing_segment = asyncio.run(
        patch_conversation_segment_text(
            FakeRequest(env, signed_headers(secret), body={"segment_id": "missing", "text": "nope"}),
            "editable",
        )
    )
    assert missing_segment.status_code == 404
    other_user_update = asyncio.run(
        patch_conversation_segment_text(
            FakeRequest(
                env,
                signed_headers(secret, "other-user"),
                body={"segment_id": "segment-1", "text": "nope"},
            ),
            "editable",
        )
    )
    assert other_user_update == {"status": "Ok"}
    assert (
        asyncio.run(get_conversation(FakeRequest(env, signed_headers(secret, "other-user")), "editable"))["transcript_segments"][0]["text"]
        == "nope"
    )
    assert (
        asyncio.run(get_conversation(FakeRequest(env, signed_headers(secret)), "editable"))["transcript_segments"][0]["text"]
        == "edited"
    )
    invalid = asyncio.run(
        patch_conversation_segment_text(
            FakeRequest(env, signed_headers(secret), body={"segment_id": "segment-1", "text": ""}),
            "editable",
        )
    )
    assert invalid.status_code == 400
    unauthorized = asyncio.run(
        patch_conversation_segment_text(
            FakeRequest(env, {}, body={"segment_id": "segment-1", "text": "nope"}),
            "editable",
        )
    )
    assert unauthorized.status_code == 401


def test_canonical_recording_existence_uses_uid_scoped_r2_and_locked_rows_fail_closed():
    secret = "conversation-secret"
    db = FakeDb()
    insert_conversation(db, uid="conversation-user", conversation_id="recorded", created_at=200)
    insert_conversation(db, uid="conversation-user", conversation_id="unrecorded", created_at=150)
    insert_conversation(db, uid="conversation-user", conversation_id="locked-recording", created_at=100, locked=1)
    bucket = FakeBucket()
    bucket.keys.add("conversation-user/recorded.wav")
    env = type("Env", (), {"APP_DB": db, "ASSETS": bucket, "INTERNAL_ASSERTION_SECRET": secret})()

    exists = asyncio.run(conversation_has_recording(FakeRequest(env, signed_headers(secret)), "recorded"))
    assert exists == {"has_recording": True}
    absent = asyncio.run(conversation_has_recording(FakeRequest(env, signed_headers(secret)), "unrecorded"))
    assert absent == {"has_recording": False}
    locked = asyncio.run(conversation_has_recording(FakeRequest(env, signed_headers(secret)), "locked-recording"))
    assert locked.status_code == 402
    missing = asyncio.run(conversation_has_recording(FakeRequest(env, signed_headers(secret)), "missing"))
    assert missing.status_code == 404
    no_bucket = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()
    unavailable = asyncio.run(conversation_has_recording(FakeRequest(no_bucket, signed_headers(secret)), "recorded"))
    assert unavailable.status_code == 503


def test_canonical_transcripts_group_imported_providers_and_fail_closed_for_locked_rows():
    secret = "conversation-secret"
    db = FakeDb()
    segments = [
        {"id": "dg-late", "text": "late", "start": 3, "end": 4, "stt_provider": "deepgram_streaming"},
        {"id": "dg-early", "text": "early", "start": 1, "end": 2, "stt_provider": "deepgram"},
        {"id": "sx", "text": "soniox", "start": 2, "end": 3, "stt_provider": "soniox_streaming"},
        {"id": "sm", "text": "speechmatics", "start": 4, "end": 5, "stt_provider": "speechmatics_streaming"},
        {"id": "wx", "text": "whisper", "start": 0, "end": 1, "stt_provider": "fal_whisperx"},
        {"id": "unknown", "text": "ignored", "start": 0, "end": 1, "stt_provider": "provider-x"},
    ]
    insert_conversation(
        db,
        uid="conversation-user",
        conversation_id="with-transcripts",
        created_at=200,
        transcript_segments=segments,
    )
    insert_conversation(db, uid="conversation-user", conversation_id="locked-transcripts", created_at=100, locked=1)
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    result = asyncio.run(get_conversation_transcripts(FakeRequest(env, signed_headers(secret)), "with-transcripts"))
    assert [item["id"] for item in result["deepgram"]] == ["dg-early", "dg-late"]
    assert [item["id"] for item in result["soniox"]] == ["sx"]
    assert [item["id"] for item in result["speechmatics"]] == ["sm"]
    assert [item["id"] for item in result["whisperx"]] == ["wx"]
    assert "unknown" not in {item["id"] for items in result.values() for item in items}
    assert asyncio.run(get_conversation_transcripts(FakeRequest(env, signed_headers(secret)), "locked-transcripts")).status_code == 402
    assert asyncio.run(get_conversation_transcripts(FakeRequest(env, signed_headers(secret)), "missing")).status_code == 404
    assert asyncio.run(get_conversation_transcripts(FakeRequest(env, {}), "with-transcripts")).status_code == 401
    assert asyncio.run(get_conversation_transcripts(FakeRequest(env, signed_headers(secret)), "x" * 257)).status_code == 400


def test_canonical_conversation_analytics_uses_d1_transcripts_and_people_names():
    secret = "conversation-secret"
    db = FakeDb()
    db.connection.execute(
        "INSERT INTO cf_people (uid, id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("conversation-user", "person-1", "Alice", 100, 100),
    )
    insert_conversation(
        db,
        uid="conversation-user",
        conversation_id="analytics",
        created_at=200,
        transcript_segments=[
            {"id": "u", "text": "one two", "start": 0, "end": 10, "is_user": True},
            {
                "id": "p",
                "text": "hello there friend",
                "start": 10,
                "end": 20,
                "is_user": False,
                "person_id": "person-1",
            },
            {"id": "s", "text": "okay", "start": 20, "end": 25, "is_user": False, "speaker_id": 2},
        ],
    )
    insert_conversation(db, uid="conversation-user", conversation_id="locked-analytics", created_at=100, locked=1)
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    result = asyncio.run(get_conversation_analytics(FakeRequest(env, signed_headers(secret)), "analytics"))
    assert result["conversation_id"] == "analytics"
    assert result["total_seconds"] == 25.0
    assert result["total_words"] == 6
    assert result["words_per_minute"] == 14.4
    assert result["speakers"][0]["speaker"] == "Alice"
    assert result["speakers"][0]["talk_seconds"] == 10.0
    assert result["speakers"][1]["speaker"] == "You"
    assert result["speakers"][2]["speaker"] == "Speaker 2"
    assert asyncio.run(get_conversation_analytics(FakeRequest(env, signed_headers(secret)), "locked-analytics")).status_code == 402
    assert asyncio.run(get_conversation_analytics(FakeRequest(env, signed_headers(secret)), "missing")).status_code == 404
    assert asyncio.run(get_conversation_analytics(FakeRequest(env, {}), "analytics")).status_code == 401


def test_canonical_conversation_events_update_is_bounded_and_preserves_index_semantics():
    secret = "conversation-secret"
    db = FakeDb()
    insert_conversation(db, uid="conversation-user", conversation_id="events", created_at=200)
    insert_conversation(db, uid="conversation-user", conversation_id="locked-events", created_at=100, locked=1)
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    updated = asyncio.run(
        patch_conversation_events(
            FakeRequest(env, signed_headers(secret), body={"events_idx": [0, 99], "values": [True, False]}),
            "events",
        )
    )
    assert updated == {"status": "Ok"}
    detail = asyncio.run(get_conversation(FakeRequest(env, signed_headers(secret)), "events"))
    assert detail["structured"]["events"] == [{"title": "event", "created": True}]
    assert (
        asyncio.run(
            patch_conversation_events(
                FakeRequest(env, signed_headers(secret), body={"events_idx": [0], "values": []}),
                "events",
            )
        ).status_code
        == 400
    )
    assert (
        asyncio.run(
            patch_conversation_events(
                FakeRequest(env, signed_headers(secret), body={"events_idx": [-1], "values": [True]}),
                "events",
            )
        ).status_code
        == 400
    )
    assert (
        asyncio.run(
            patch_conversation_events(
                FakeRequest(env, signed_headers(secret), body={"events_idx": [0], "values": [True]}),
                "locked-events",
            )
        ).status_code
        == 402
    )
    assert (
        asyncio.run(
            patch_conversation_events(
                FakeRequest(env, signed_headers(secret), body={"events_idx": [0], "values": [True]}),
                "missing",
            )
        ).status_code
        == 404
    )
    assert (
        asyncio.run(
            patch_conversation_events(
                FakeRequest(env, {}, body={"events_idx": [0], "values": [True]}),
                "events",
            )
        ).status_code
        == 401
    )


def test_canonical_conversation_action_item_state_updates_projection_and_standalone_rows():
    secret = "conversation-secret"
    db = FakeDb()
    insert_conversation(db, uid="conversation-user", conversation_id="action-items", created_at=200)
    insert_conversation(db, uid="conversation-user", conversation_id="locked-action-items", created_at=100, locked=1)
    db.connection.execute(
        "INSERT INTO cf_action_items (uid, id, description, status, completed, conversation_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("conversation-user", "item-1", "task", "active", 0, "action-items", 200, 200),
    )
    db.connection.commit()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    updated = asyncio.run(
        patch_conversation_action_items(
            FakeRequest(env, signed_headers(secret), body={"items_idx": [0, 99], "values": [True, False]}),
            "action-items",
        )
    )
    assert updated == {"status": "Ok"}
    detail = asyncio.run(get_conversation(FakeRequest(env, signed_headers(secret)), "action-items"))
    assert detail["structured"]["action_items"][0]["completed"] is True
    standalone = db.connection.execute(
        "SELECT completed, status FROM cf_action_items WHERE uid = ? AND id = ?",
        ("conversation-user", "item-1"),
    ).fetchone()
    assert tuple(standalone) == (1, "completed")
    assert (
        asyncio.run(
            patch_conversation_action_items(
                FakeRequest(env, signed_headers(secret), body={"items_idx": [0], "values": []}),
                "action-items",
            )
        ).status_code
        == 400
    )
    assert (
        asyncio.run(
            patch_conversation_action_items(
                FakeRequest(env, signed_headers(secret), body={"items_idx": [-1], "values": [True]}),
                "action-items",
            )
        ).status_code
        == 400
    )
    assert (
        asyncio.run(
            patch_conversation_action_items(
                FakeRequest(env, signed_headers(secret), body={"items_idx": [0], "values": [True]}),
                "locked-action-items",
            )
        ).status_code
        == 402
    )
    assert (
        asyncio.run(
            patch_conversation_action_items(
                FakeRequest(env, signed_headers(secret), body={"items_idx": [0], "values": [True]}),
                "missing",
            )
        ).status_code
        == 404
    )
    assert (
        asyncio.run(
            patch_conversation_action_items(
                FakeRequest(env, {}, body={"items_idx": [0], "values": [True]}),
                "action-items",
            )
        ).status_code
        == 401
    )


def test_conversation_projection_write_is_idempotent_and_bounded():
    secret = "conversation-secret"
    db = FakeDb()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()
    body = {
        "id": "write-1",
        "created_at": "2026-08-28T10:00:00Z",
        "started_at": "2026-08-28T10:00:00Z",
        "finished_at": "2026-08-28T10:01:00Z",
        "source": "desktop",
        "language": "en",
        "structured": {"title": "First", "overview": "Draft"},
        "transcript_segments": [{"id": "s-1", "text": "hello", "start": 0, "end": 1, "is_user": True}],
        "private_cloud_sync_enabled": True,
    }
    first = asyncio.run(store_conversation_projection(FakeRequest(env, signed_headers(secret), body=body)))
    assert first == {"conversation_id": "write-1", "status": "stored"}
    second = asyncio.run(
        store_conversation_projection(
            FakeRequest(env, signed_headers(secret), body={**body, "structured": {"title": "Updated"}})
        )
    )
    assert second == first
    detail = asyncio.run(get_conversation(FakeRequest(env, signed_headers(secret)), "write-1"))
    assert detail["structured"]["title"] == "Updated"
    assert detail["private_cloud_sync_enabled"] is True

    invalid = asyncio.run(
        store_conversation_projection(
            FakeRequest(env, signed_headers(secret), body={**body, "status": "unknown"})
        )
    )
    assert invalid.status_code == 400


def test_canonical_conversation_metadata_mutations_are_uid_scoped():
    secret = "conversation-secret"
    db = FakeDb()
    insert_conversation(db, uid="conversation-user", conversation_id="conv-1", created_at=200)
    insert_conversation(db, uid="other-user", conversation_id="conv-1", created_at=300)
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()

    renamed = asyncio.run(
        patch_conversation_title(
            FakeRequest(env, signed_headers(secret), query={"title": "Updated title"}),
            "conv-1",
        )
    )
    assert renamed["status"] == "Ok"
    assert renamed["conversation"]["structured"]["title"] == "Updated title"

    starred = asyncio.run(
        set_conversation_starred(
            FakeRequest(env, signed_headers(secret), query={"starred": "true"}),
            "conv-1",
        )
    )
    assert starred["conversation"]["starred"] is True
    assert len(asyncio.run(list_conversations(FakeRequest(env, signed_headers(secret), {"starred": "true"})))) == 1

    invalid_title = asyncio.run(
        patch_conversation_title(FakeRequest(env, signed_headers(secret), query={"title": " "}), "conv-1")
    )
    assert invalid_title.status_code == 400
    invalid_starred = asyncio.run(
        set_conversation_starred(FakeRequest(env, signed_headers(secret), query={"starred": "maybe"}), "conv-1")
    )
    assert invalid_starred.status_code == 400
    missing = asyncio.run(
        patch_conversation_title(
            FakeRequest(env, signed_headers(secret, "other-user"), query={"title": "no leak"}),
            "conv-1",
        )
    )
    assert missing["conversation"]["structured"]["title"] == "no leak"
    assert asyncio.run(get_conversation(FakeRequest(env, signed_headers(secret)), "conv-1"))["structured"]["title"] == "Updated title"
