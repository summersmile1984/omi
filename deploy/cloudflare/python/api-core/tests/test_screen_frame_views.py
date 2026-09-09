"""Registered HTTP routes, unchanged domain selection and all production D1 SQL."""

import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from internal_auth import create_request_context
from screen_frame_content import _chunks, router as content_router
from screen_frame_views import EMPTY, router


class Statement:
    def __init__(self, db, sql):
        self.db, self.sql, self.args = db, sql, ()

    def bind(self, *args):
        self.args = args
        return self

    def execute(self):
        if self.db.before:
            callback, self.db.before = self.db.before, None
            callback(self)
        return self.db.connection.execute(self.sql, self.args)

    async def first(self):
        row = self.execute().fetchone()
        return dict(row) if row else None

    async def run(self):
        return {'meta': {'changes': self.execute().rowcount}}


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.before = None
        for path in sorted((Path(__file__).parents[3] / 'migrations/app').glob('*.sql')):
            self.connection.executescript(path.read_text())

    def prepare(self, sql):
        return Statement(self, sql)


class Reader:
    def __init__(self):
        self.values = iter([b'\xff\xd8', b'fixture', b'\xff\xd9'])
        self.cancelled, self.released = False, False

    async def read(self):
        value = next(self.values, None)
        return SimpleNamespace(done=value is None, value=value)

    async def cancel(self):
        self.cancelled = True

    def releaseLock(self):
        self.released = True


class Writer:
    # Only the transport is controlled. Actual writer capability/privacy/R2
    # behavior lives in screen-frame-writer.test.ts and screen-frame-r2.test.mjs.
    def __init__(self):
        self.status, self.calls, self.reader = 200, [], Reader()

    async def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(status=self.status, body=SimpleNamespace(getReader=lambda: self.reader))


@pytest.fixture
def target():
    db, writer = Database(), Writer()
    env = SimpleNamespace(
        APP_DB=db,
        SCREEN_FRAME_WRITER=writer,
        SCREEN_FRAME_SIGNING_SECRET='screen-key-for-tests-' * 3,
        INTERNAL_ASSERTION_SECRET='internal-fixture',
        PUBLIC_API_BASE_URL='https://api.eddy.test',
    )
    app = FastAPI()
    app.include_router(router)
    app.include_router(content_router)

    @app.middleware('http')
    async def environment(request, call_next):
        request.scope['env'] = env
        return await call_next(request)

    async def call(method, path='/v1/conversations/meeting/screenshots', *, uid='owner', body=None, headers=None):
        auth = {}
        if uid:
            encoded, signature = create_request_context(
                uid,
                env.INTERNAL_ASSERTION_SECRET,
                audience='api-core',
                method=method,
                path=urlsplit(path).path,
                request_id='screenshot-fixture',
                authority='better-auth',
            )
            auth = {'x-omi-auth-context': encoded, 'x-omi-internal-signature': signature}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=env.PUBLIC_API_BASE_URL) as client:
            return await client.request(method, path, json=body, headers=headers if headers is not None else auth)

    for uid in ('owner', 'other'):
        db.connection.execute(
            "INSERT INTO cf_conversations (uid, id, created_at, status) VALUES (?, 'meeting', 1, 'completed')", (uid,)
        )
        db.connection.execute("INSERT INTO cf_screen_frame_sets (uid, conversation_id) VALUES (?, 'meeting')", (uid,))
    yield SimpleNamespace(db=db, env=env, call=lambda *a, **kw: asyncio.run(call(*a, **kw)), writer=writer)
    db.connection.close()


def publish(target, *, uid='owner', count=3):
    db, attempt, now = target.db.connection, str(uuid4()), int(time.time())
    db.execute(
        "INSERT INTO cf_screen_frame_attempts (uid, conversation_id, attempt_id, fingerprint, epoch, expires_at) "
        "VALUES (?, 'meeting', ?, ?, 0, ?)",
        (uid, attempt, 'a' * 64, now + 600),
    )
    frames = []
    for n in range(count):
        frame = {
            'id': str(uuid4()),
            'captured_at': f'2026-09-06T01:00:0{n}Z',
            'caption': f'Frame {n}',
            'labels': ['meeting'],
            'source_badge': 'slides',
            'banner_suitability': (n + 1) / 10,
            'width': 1600,
            'height': 900,
            'ground': {'stops': ['#123456', '#334455'], 'is_neutral': False},
            'role': 'strip',
            'rank': n,
        }
        if n == count - 1:
            frame.update(role='banner', rank=0, banner_suitability=0.8)
        elif n == count - 2:
            frame['banner_suitability'] = 0.5
        db.execute(
            'INSERT INTO cf_screen_frame_writes (jti, uid, conversation_id, attempt_id, epoch, expires_at, phase, '
            "canonical_sha256, thumbnail_sha256, metadata_json) VALUES (?, ?, 'meeting', ?, 0, ?, 'writing', ?, ?, ?)",
            (frame['id'], uid, attempt, now + 600, 'a' * 64, 'b' * 64, json.dumps(frame)),
        )
        db.execute("UPDATE cf_screen_frame_writes SET phase = 'ready' WHERE jti = ?", (frame['id'],))
        frames.append(frame)
    db.execute(
        'UPDATE cf_screen_frame_sets SET frames_json = ?, revision = 1, adjudicated_at = ? '
        "WHERE uid = ? AND conversation_id = 'meeting'",
        (json.dumps(frames), '2026-09-06T01:01:00Z', uid),
    )
    return frames


def share(target):
    db = target.db.connection
    db.execute("UPDATE cf_conversations SET visibility = 'shared' WHERE uid = 'owner'")
    db.execute(
        "INSERT INTO cf_shared_conversation_index (conversation_id, uid, visibility, updated_at) "
        "VALUES ('meeting', 'owner', 'shared', 1)"
    )


def capability(url, secret):
    token = parse_qs(urlsplit(url).query)['token'][0]
    purpose, body, signature = token.split('.')
    assert purpose == 'screen-frame-content-v1'
    expected = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), (purpose + '.' + body).encode(), hashlib.sha256).digest()
    )
    assert hmac.compare_digest(signature, expected.decode().rstrip('='))
    return json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))


def test_legacy_default_owner_isolation_and_request_bound_auth(target):
    assert target.call('GET', '/v1/screen-frame-egress/settings').json() == {'meeting_note_screenshots_enabled': True}
    assert target.call('GET').json() == EMPTY.model_dump(mode='json')
    publish(target)
    assert target.call('GET', uid='other').json()['banner'] is None
    assert target.call('GET', uid=None).status_code == 401
    assert target.call('DELETE', '/v1/conversations/missing/screenshots').status_code == 404
    encoded, signature = create_request_context(
        'owner',
        target.env.INTERNAL_ASSERTION_SECRET,
        audience='api-core',
        method='GET',
        path='/v1/conversations/meeting/screenshots',
        request_id='fixture',
    )
    assert (
        target.call(
            'DELETE', headers={'x-omi-auth-context': encoded, 'x-omi-internal-signature': signature}
        ).status_code
        == 401
    )


def test_owner_wire_and_signed_content_do_not_expose_internal_approvals(target):
    frames = publish(target)
    result = target.call('GET')
    assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
    value = result.json()
    assert value['revision'] == 1 and value['adjudicated_at'] == '2026-09-06T01:01:00Z'
    assert value['banner']['id'] == frames[-1]['id'] and len(value['strip']) == 2
    for variant in ('content', 'thumbnail'):
        url = value['banner'][variant + '_url']
        assert url.startswith(target.env.PUBLIC_API_BASE_URL + '/v1/screen-frame-content?')
        claims = capability(url, target.env.SCREEN_FRAME_SIGNING_SECRET)
        assert claims == {
            'uid': 'owner',
            'frame_id': frames[-1]['id'],
            'variant': variant,
            'access': 'owner',
            'expires_at': claims['expires_at'],
        }
        assert 3595 <= claims['expires_at'] - time.time() <= 3600
    assert 'canonical_sha256' not in result.text and 'approval' not in result.text


def test_account_setting_hides_owner_and_shared_without_erasing_approved_frames(target):
    publish(target)
    share(target)
    path = '/v1/screen-frame-egress/settings'
    assert target.call('PATCH', path, body={'meeting_note_screenshots_enabled': False}).status_code == 200
    for path in ('/v1/conversations/meeting/screenshots', '/v1/conversations/meeting/shared/screenshots'):
        assert target.call('GET', path).json() == EMPTY.model_dump(mode='json')
    row = target.db.connection.execute("SELECT * FROM cf_screen_frame_sets WHERE uid = 'owner'").fetchone()
    assert row['epoch'] == 1 and row['revision'] == 1 and len(json.loads(row['frames_json'])) == 3
    assert (
        target.call(
            'PATCH', '/v1/screen-frame-egress/settings', body={'meeting_note_screenshots_enabled': True}
        ).status_code
        == 200
    )
    assert target.call('GET').json()['banner'] is not None


def test_public_lookup_uses_authoritative_share_index_and_revocation_does_not_bump_revision(target):
    frames = publish(target)
    publish(target, uid='other')
    path = '/v1/conversations/meeting/shared/screenshots'
    assert target.call('GET', path, uid=None).json() == EMPTY.model_dump(mode='json')
    share(target)
    shared = target.call('GET', path, uid=None).json()
    assert shared['banner']['id'] == frames[-1]['id']
    assert capability(shared['banner']['content_url'], target.env.SCREEN_FRAME_SIGNING_SECRET)['access'] == 'shared'
    updated = target.call('PATCH', '/v1/conversations/meeting/screenshot-sharing', body={'enabled': False})
    assert updated.json()['revision'] == 1
    assert target.call('GET', path, uid=None).json() == EMPTY.model_dump(mode='json')
    assert target.call('GET', '/v1/conversations/unknown/shared/screenshots', uid=None).json() == EMPTY.model_dump(
        mode='json'
    )
    assert target.call('GET').json()['banner'] is not None


def test_delete_promotes_only_surviving_approved_frame_and_marks_old_bytes_for_cleanup(target):
    frames = publish(target)
    deleted = target.call('DELETE', '/v1/conversations/meeting/screenshots/' + frames[-1]['id'])
    assert deleted.status_code == 200
    value = deleted.json()
    assert value['revision'] == 2 and value['banner']['id'] == frames[-2]['id']
    assert [item['id'] for item in value['strip']] == [frames[0]['id']]
    phase = target.db.connection.execute(
        'SELECT phase FROM cf_screen_frame_writes WHERE jti = ?', (frames[-1]['id'],)
    ).fetchone()[0]
    assert phase == 'cleanup'
    assert target.call('DELETE', '/v1/conversations/meeting/screenshots/' + frames[-1]['id']).status_code == 404
    assert target.call('DELETE').json()['revision'] == 3
    assert target.call('DELETE').json()['revision'] == 4
    assert target.call('GET').json()['adjudicated_at'] == '2026-09-06T01:01:00Z'


def test_delete_retries_revision_race_and_never_resurrects_a_concurrent_removal(target):
    frames = publish(target)
    fired = []

    def interleave(statement):
        if not statement.sql.startswith('UPDATE cf_screen_frame_sets SET frames_json'):
            target.db.before = interleave
            return
        fired.append(True)
        target.db.connection.execute(
            'UPDATE cf_screen_frame_sets SET frames_json = ?, revision = revision + 1, epoch = epoch + 1 '
            "WHERE uid = 'owner'",
            (json.dumps(frames[1:]),),
        )

    target.db.before = interleave
    result = target.call('DELETE', '/v1/conversations/meeting/screenshots/' + frames[-1]['id'])
    assert fired and result.status_code == 200 and result.json()['revision'] == 3
    assert result.json()['banner']['id'] == frames[1]['id'] and result.json()['strip'] == []
    assert (
        target.db.connection.execute("SELECT count(*) FROM cf_screen_frame_writes WHERE phase = 'cleanup'").fetchone()[
            0
        ]
        == 2
    )


def test_disable_between_survivor_read_and_response_hides_the_snapshot(target):
    publish(target)

    def interleave(statement):
        if 'SELECT COALESCE(' not in statement.sql:
            target.db.before = interleave
            return
        target.db.connection.execute("INSERT INTO cf_screen_frame_settings (uid, enabled) VALUES ('owner', 0)")

    target.db.before = interleave
    assert target.call('GET').json() == EMPTY.model_dump(mode='json')


def test_corrupt_frame_is_skipped_and_legacy_palette_uses_sanitized_fallback(target, capsys):
    frames = publish(target)
    frames[0]['caption'] = 'x' * 161
    frames[1]['ground'] = None
    target.db.connection.execute(
        "UPDATE cf_screen_frame_sets SET frames_json = ? WHERE uid = 'owner'", (json.dumps(frames),)
    )
    response = target.call('GET')
    assert response.status_code == 200 and len(response.json()['strip']) == 1
    assert response.json()['strip'][0]['ground'] == {'stops': ['#5A5D66', '#33363D'], 'is_neutral': True}
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(events) == 2 and all(event['reason'] == 'malformed_doc' for event in events)
    assert 'owner' not in json.dumps(events) and 'Frame' not in json.dumps(events)


@pytest.mark.parametrize('secret', [None, '', 'short', 'internal-fixture'])
def test_misconfigured_signing_fails_without_omitting_the_whole_set(target, secret):
    publish(target)
    target.env.SCREEN_FRAME_SIGNING_SECRET = secret
    assert target.call('GET').status_code == 503


def test_strict_wire_rejects_client_verdict_and_account_fence_blocks_mutation(target):
    assert (
        target.call(
            'PATCH',
            '/v1/screen-frame-egress/settings',
            body={'meeting_note_screenshots_enabled': True, 'approved': True},
        ).status_code
        == 422
    )
    publish(target)
    share(target)
    target.db.connection.execute(
        "INSERT INTO cf_account_deletion_tombstones (uid, completed_at, expires_at) VALUES ('owner', 1, 9999999999)"
    )
    assert target.call('GET').status_code == 404
    assert target.call('GET', '/v1/conversations/meeting/shared/screenshots', uid=None).json() == EMPTY.model_dump(
        mode='json'
    )
    assert (
        target.call(
            'PATCH', '/v1/screen-frame-egress/settings', body={'meeting_note_screenshots_enabled': True}
        ).status_code
        == 503
    )
    assert target.call('DELETE').status_code == 404


def test_proxy_forwards_only_the_capability_and_streams_the_writer_response(target):
    publish(target)
    url = target.call('GET').json()['banner']['content_url']
    response = target.call('GET', url, uid=None, headers={'cookie': 'private-session', 'x-untrusted': 'never-forward'})
    assert response.status_code == 200 and response.content == b'\xff\xd8fixture\xff\xd9'
    assert response.headers['content-type'] == 'image/jpeg' and response.headers['cache-control'] == 'no-store'
    called, kwargs = target.writer.calls[0]
    assert called.startswith('https://screen-frame-writer/v1/screen-frame-content?token=') and kwargs == {}
    assert target.writer.reader.released and not target.writer.reader.cancelled
    target.writer.status = 404
    assert target.call('GET', url, uid=None).status_code == 404
    before = len(target.writer.calls)
    assert target.call('GET', '/v1/screen-frame-content?token=' + 'x' * 4097, uid=None).status_code == 404
    assert len(target.writer.calls) == before


@pytest.mark.parametrize('cancel_fails', [False, True])
def test_stream_disconnect_cancels_the_reader(cancel_fails):
    reader = Reader()
    if cancel_fails:

        async def failed_cancel():
            reader.cancelled = True
            raise RuntimeError('upstream cancellation failed')

        reader.cancel = failed_cancel

    async def exercise():
        stream = _chunks(SimpleNamespace(body=SimpleNamespace(getReader=lambda: reader)))
        assert await anext(stream) == b'\xff\xd8'
        if cancel_fails:
            with pytest.raises(RuntimeError, match='upstream cancellation failed'):
                await stream.aclose()
        else:
            await stream.aclose()

    asyncio.run(exercise())
    assert reader.cancelled and reader.released
