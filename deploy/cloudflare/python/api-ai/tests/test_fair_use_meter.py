from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from fair_use_meter import content_source_id, record_fair_use_usage, speech_ms_from_transcription  # noqa: E402


def test_speech_ms_unions_segments_and_excludes_invalid_intervals():
    assert (
        speech_ms_from_transcription(
            {
                "segments": [
                    {"start": 0, "end": 1.5, "text": "one"},
                    {"start": 1, "end": 2, "text": "overlap"},
                    {"start": 3.25, "end": 4, "text": "two"},
                    {"start": 5, "end": 4, "text": "invalid"},
                    {"start": 6, "end": 7, "text": ""},
                ]
            }
        )
        == 2_750
    )


def test_speech_ms_uses_words_when_segments_are_empty():
    assert speech_ms_from_transcription({"segments": [], "words": [{"start": 0.1, "end": 0.6, "word": "hi"}]}) == 500


def test_content_source_id_uses_explicit_operation_identity_or_content_fallback():
    first = content_source_id("workers-ai", b"audio", "retry-1")
    assert first == content_source_id("workers-ai", b"audio", "retry-1")
    assert first == content_source_id("workers-ai", b"different", "retry-1")
    assert first != content_source_id("workers-ai", b"audio", "retry-2")
    assert content_source_id("workers-ai", b"audio") != content_source_id("workers-ai", b"different")


@dataclass
class CapturedQuery:
    sql: str = ""
    values: tuple[object, ...] = ()
    runs: int = 0

    def bind(self, *values: object):
        self.values = values
        return self

    async def run(self):
        self.runs += 1


class FakeDatabase:
    def __init__(self):
        self.query = CapturedQuery()

    def prepare(self, sql: str):
        self.query.sql = sql
        return self.query


def test_records_revisioned_idempotent_usage():
    database = FakeDatabase()
    env = type("Env", (), {"APP_DB": database})()

    recorded = asyncio.run(
        record_fair_use_usage(
            env,
            uid="user-1",
            source_kind="sync_fresh",
            source_id="async:job-1",
            occurred_at=100,
            speech_ms=1_250,
            revision=3,
        )
    )

    assert recorded is True
    assert "ON CONFLICT(uid, source_kind, source_id)" in database.query.sql
    assert "excluded.revision >=" in database.query.sql
    assert database.query.values[:6] == ("user-1", "sync_fresh", "async:job-1", 100, 1_250, 0)
    assert database.query.values[7] == 3
    assert database.query.runs == 1


def test_does_not_write_empty_speech():
    database = FakeDatabase()
    env = type("Env", (), {"APP_DB": database})()

    assert (
        asyncio.run(
            record_fair_use_usage(
                env,
                uid="user-1",
                source_kind="sync_fresh",
                source_id="async:job-1",
                speech_ms=0,
            )
        )
        is False
    )
    assert database.query.runs == 0
