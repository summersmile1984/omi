import asyncio
import json
from types import SimpleNamespace

from test_conversation_finalization_routes import (  # noqa: F401
    FakeDb,
    FakeQueue,
    FakeRequest,
    SECRET,
    auth_headers,
)
from conversation_merge_routes import (  # noqa: E402
    _merge_segments,
    merge_conversations,
    process_conversation_merge,
)

UID = "merge-user"


def environment():
    db = FakeDb()
    queue = FakeQueue()
    env = SimpleNamespace(APP_DB=db, JOBS=queue, INTERNAL_ASSERTION_SECRET=SECRET)
    rows = [
        ("conversation-a", 100, 110, [{"text": "first", "start": 0, "end": 2, "speaker": "A"}]),
        ("conversation-b", 120, 130, [{"text": "second", "start": 0, "end": 3, "speaker": "B"}]),
    ]
    for conversation_id, started, finished, segments in rows:
        db.connection.execute(
            "INSERT INTO cf_conversations "
            "(uid, id, created_at, updated_at, started_at, finished_at, source, language, status, visibility, "
            "structured_json, transcript_segments_json, photos_json, audio_files_json, external_data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, 'desktop', 'en', 'completed', 'private', '{}', ?, '[]', '[]', '{}')",
            (UID, conversation_id, started, finished, started, finished, json.dumps(segments)),
        )
    db.connection.commit()
    return env, queue


def test_merge_offsets_segments_and_preserves_gap():
    merged = _merge_segments(
        [
            {
                "started_at": 100,
                "finished_at": 110,
                "transcript_segments_json": json.dumps([{"text": "a", "start": 0, "end": 2}]),
            },
            {
                "started_at": 120,
                "finished_at": 130,
                "transcript_segments_json": json.dumps([{"text": "b", "start": 0, "end": 3}]),
            },
        ]
    )
    assert [segment["text"] for segment in merged] == ["a", "b"]
    assert merged[1]["start"] == 12
    assert merged[1]["end"] == 15


def test_merge_admission_is_idempotent_and_queues_one_job():
    env, queue = environment()
    request = FakeRequest(
        env,
        headers=auth_headers(UID),
        body={"conversation_ids": ["conversation-a", "conversation-b"], "reprocess": False},
    )
    first = asyncio.run(merge_conversations(request))
    second = asyncio.run(merge_conversations(request))

    assert first["status"] == second["status"] == "merging"
    assert first["job_id"] == second["job_id"]
    assert len(queue.messages) == 1
    states = env.APP_DB.connection.execute(
        "SELECT status, merge_job_id FROM cf_conversations WHERE uid = ? ORDER BY id", (UID,)
    ).fetchall()
    assert [tuple(row) for row in states] == [("merging", first["job_id"]), ("merging", first["job_id"])]


def test_merge_processor_replaces_sources_and_persists_result(monkeypatch):
    env, queue = environment()
    admission = asyncio.run(
        merge_conversations(
            FakeRequest(
                env,
                headers=auth_headers(UID),
                body={"conversation_ids": ["conversation-a", "conversation-b"], "reprocess": True},
            )
        )
    )

    async def fake_targets(_env, _uid):
        return [], None

    async def fake_enrichment(_env, _transcript, _language):
        return {
            "structured": {"title": "Merged", "action_items": [{"description": "follow up"}]},
            "memories": ["merged memory"],
            "discarded": False,
        }

    async def fake_publish(_env, *, uid, source_kind, source_id):
        return None

    async def fake_audio(_env, _uid, _rows, result_id):
        return [], None

    monkeypatch.setattr("conversation_merge_routes._fanout_targets", fake_targets)
    monkeypatch.setattr("conversation_merge_routes._enrichment", fake_enrichment)
    monkeypatch.setattr("conversation_merge_routes._copy_audio_metadata", fake_audio)
    monkeypatch.setattr("developer_conversation_create_routes.publish_vector_projection", fake_publish)

    job = env.APP_DB.connection.execute(
        "SELECT job_id, merge_revision FROM cf_conversation_merge_jobs WHERE uid = ?", (UID,)
    ).fetchone()
    result = asyncio.run(
        process_conversation_merge(
            FakeRequest(
                env,
                headers=auth_headers(UID, authority="internal"),
                body={
                    "job_id": job[0],
                    "conversation_ids": ["conversation-a", "conversation-b"],
                    "revision": job[1],
                    "reprocess": True,
                },
            )
        )
    )

    assert result["status"] == "completed"
    target_id = result["id"]
    target = env.APP_DB.connection.execute(
        "SELECT status, transcript_segments_json, visibility FROM cf_conversations WHERE uid = ? AND id = ?",
        (UID, target_id),
    ).fetchone()
    assert target[0] == "completed"
    assert [item["text"] for item in json.loads(target[1])] == ["first", "second"]
    assert target[2] == "private"
    assert (
        env.APP_DB.connection.execute(
            "SELECT COUNT(*) FROM cf_conversations WHERE uid = ? AND id IN ('conversation-a', 'conversation-b')", (UID,)
        ).fetchone()[0]
        == 0
    )
    assert (
        env.APP_DB.connection.execute("SELECT status FROM cf_conversation_merge_jobs WHERE uid = ?", (UID,)).fetchone()[
            0
        ]
        == "completed"
    )
    assert queue.messages[0]["kind"] == "conversation_merge"
