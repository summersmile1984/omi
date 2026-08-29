from __future__ import annotations

from datetime import datetime, timezone
import json
import stat
from types import SimpleNamespace

import pytest

from scripts import export_cloudflare_x_posts as exporter


class FakeSnapshot:
    def __init__(self, post_id: str, payload: object):
        self.id = post_id
        self._payload = payload

    def to_dict(self):
        return self._payload


class FakeCollection:
    def __init__(self, rows_by_uid: dict[str, list[FakeSnapshot]], uid: str | None = None):
        self.rows_by_uid = rows_by_uid
        self.uid = uid

    def document(self, uid: str):
        return SimpleNamespace(
            collection=lambda name: FakeCollection(self.rows_by_uid, uid) if name == 'x_posts' else None
        )

    def stream(self):
        return iter(self.rows_by_uid.get(self.uid or '', []))


class FakeClient:
    def __init__(self, rows_by_uid: dict[str, list[FakeSnapshot]]):
        self.rows_by_uid = rows_by_uid

    def collection(self, name: str):
        assert name == 'users'
        return FakeCollection(self.rows_by_uid)


def test_collect_records_whitelists_and_stably_orders_firestore_posts() -> None:
    client = FakeClient(
        {
            'user-b': [
                FakeSnapshot(
                    'tweet-2',
                    {
                        'id': 'tweet-2',
                        'text': 'Workers deployment notes',
                        'kind': 'bookmark',
                        'lang': 'en',
                        'public_metrics': {'like_count': 3},
                        'created_at': datetime(2026, 8, 28, 10, tzinfo=timezone.utc),
                        'oauth_token': 'must-not-be-exported',
                    },
                )
            ],
            'user-a': [
                FakeSnapshot(
                    'tweet-1',
                    {
                        'text': 'Cloudflare 向量回填',
                        'kind': 'tweet',
                        'created_at': 1_787_911_200,
                        'updated_at': 1_787_911_260,
                        'memory_extraction_status': 'completed',
                    },
                )
            ],
        }
    )

    records = exporter.collect_records(client, ['user-b', 'user-a', 'user-a'], max_rows=10)

    assert [(record['row']['uid'], record['row']['id']) for record in records] == [
        ('user-a', 'tweet-1'),
        ('user-b', 'tweet-2'),
    ]
    second = records[1]['row']
    assert second['metrics'] == {'like_count': 3}
    assert second['updated_at'] == '2026-08-28T10:00:00Z'
    assert 'oauth_token' not in second
    payload = exporter.render_jsonl(records)
    assert payload.endswith(b'\n')
    assert len(payload.decode('utf-8').splitlines()) == 2


def test_export_rejects_identity_content_and_row_limit_violations() -> None:
    valid = {
        'text': 'valid post',
        'kind': 'tweet',
        'created_at': 1,
    }
    with pytest.raises(ValueError, match='field id'):
        exporter.normalize_snapshot('user-1', FakeSnapshot('post-1', {**valid, 'id': 'post-2'}))
    with pytest.raises(ValueError, match='text'):
        exporter.normalize_snapshot('user-1', FakeSnapshot('post-1', {**valid, 'text': ' '}))
    with pytest.raises(ValueError, match='kind'):
        exporter.normalize_snapshot('user-1', FakeSnapshot('post-1', {**valid, 'kind': 'retweet'}))
    with pytest.raises(ValueError, match='created_at'):
        exporter.normalize_snapshot('user-1', FakeSnapshot('post-1', {**valid, 'created_at': 'not-a-timestamp'}))
    with pytest.raises(ValueError, match='post id'):
        exporter.normalize_snapshot('user-1', FakeSnapshot(None, valid))
    with pytest.raises(ValueError, match='row limit'):
        exporter.collect_records(
            FakeClient({'user-1': [FakeSnapshot('post-1', valid), FakeSnapshot('post-2', valid)]}),
            ['user-1'],
            max_rows=1,
        )


def test_private_export_is_exclusive_and_mode_0600(tmp_path) -> None:
    destination = tmp_path / 'x-posts.jsonl'
    payload = exporter.render_jsonl([{'table': 'cf_x_posts', 'row': {'uid': 'u', 'id': 'p'}}])

    exporter.write_private_export(destination, payload)

    assert destination.read_bytes() == payload
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        exporter.write_private_export(destination, payload)
    assert json.loads(destination.read_text(encoding='utf-8').strip())['table'] == 'cf_x_posts'
