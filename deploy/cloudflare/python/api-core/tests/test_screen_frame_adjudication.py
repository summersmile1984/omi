"""Candidate HTTP behavior through real canonicalization and production D1 SQL."""

import asyncio
import base64
from copy import deepcopy
import hashlib
import hmac
import io
import inspect
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import FastAPI
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from internal_auth import create_request_context
from screen_frame_adjudication import router
from screen_frame_views import router as views_router
from screen_frames_prompt import _PRIVACY_PROMPT
from test_screen_frame_views import Database
from test_frame_image_transform import native_images

PATH = '/v1/screen-frame-egress/adjudications'
APPROVED = {
    'outcome': 'approved_clean',
    'reject_reason': None,
    'caption': 'Meeting slide',
    'labels': ['meeting'],
    'source_badge': 'slides',
    'banner_suitability': 0.8,
}


class D1(Database):
    def __init__(self):
        super().__init__()
        self.before_batch = None

    async def batch(self, statements):
        if self.before_batch:
            callback, self.before_batch = self.before_batch, None
            result = callback(statements)
            if inspect.isawaitable(result):
                await result
        self.connection.execute('SAVEPOINT cf_batch')
        try:
            results = [{'success': True, 'meta': {'changes': statement.execute().rowcount}} for statement in statements]
            self.connection.execute('RELEASE cf_batch')
            return results
        except Exception:
            self.connection.execute('ROLLBACK TO cf_batch')
            self.connection.execute('RELEASE cf_batch')
            raise


class AI:
    def __init__(self):
        self.calls, self.results, self.hook = [], [], None

    async def run(self, model, payload):
        self.calls.append((model, payload))
        if self.hook:
            self.hook()
        result = self.results.pop(0) if self.results else deepcopy(APPROVED)
        if isinstance(result, Exception):
            raise result
        return {
            'candidates': [{'content': {'parts': [{'text': json.dumps(result)}]}, 'finishReason': 'STOP'}],
            'usageMetadata': {'promptTokenCount': 120, 'candidatesTokenCount': 30},
        }


class Writer:
    def __init__(self, db, secret):
        self.db, self.secret, self.ready, self.status, self.calls, self.hook = db, secret, 200, 201, [], None

    async def fetch(self, url, **kwargs):
        if url.endswith('/ready'):
            return SimpleNamespace(status=self.ready)
        body = json.loads(kwargs['body'])
        prefix, payload, signature = body['approval'].split('.')
        assert prefix == 'screen-frame-approval-v1'
        expected = (
            base64.urlsafe_b64encode(
                hmac.new(self.secret.encode(), (prefix + '.' + payload).encode(), hashlib.sha256).digest()
            )
            .decode()
            .rstrip('=')
        )
        assert hmac.compare_digest(signature, expected)
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        jpeg, thumbnail = base64.b64decode(body['jpeg_base64']), base64.b64decode(body['thumbnail_base64'])
        assert hashlib.sha256(jpeg).hexdigest() == claims['canonical_sha256']
        assert hashlib.sha256(thumbnail).hexdigest() == claims['thumbnail_sha256']
        self.calls.append((claims, jpeg, thumbnail))
        if self.hook:
            self.hook()
        if self.status == 201:
            self.db.connection.execute(
                'INSERT INTO cf_screen_frame_writes (jti, uid, conversation_id, attempt_id, epoch, expires_at, phase, '
                "canonical_sha256, thumbnail_sha256, metadata_json) VALUES (?, ?, ?, ?, ?, ?, 'writing', ?, ?, ?)",
                (
                    claims['jti'],
                    claims['uid'],
                    claims['conversation_id'],
                    claims['attempt_id'],
                    claims['epoch'],
                    claims['expires_at'],
                    claims['canonical_sha256'],
                    claims['thumbnail_sha256'],
                    json.dumps(claims['metadata']),
                ),
            )
            self.db.connection.execute(
                "UPDATE cf_screen_frame_writes SET phase = 'ready' WHERE jti = ?", (claims['jti'],)
            )

        async def response():
            return {'frame_id': claims['jti']}

        return SimpleNamespace(status=self.status, json=response)


def candidate(n=0, raw=None):
    if raw is None:
        image = io.BytesIO()
        Image.new('RGB', (1920, 1080), (25 + n, 55, 80)).save(image, format='PNG')
        raw = image.getvalue()
    return {
        'client_frame_id': 'candidate-' + str(n),
        'captured_at': f'2026-09-06T01:00:0{n}Z',
        'mime_type': 'image/png',
        'declared_width': 1920,
        'declared_height': 1080,
        'sha256_base64': base64.b64encode(hashlib.sha256(raw).digest()).decode(),
        'bytes_base64': base64.b64encode(raw).decode(),
    }


def body(*candidates):
    return {
        'schema_version': 1,
        'attempt_id': str(uuid4()),
        'purpose': 'meeting_note_v1',
        'subject': {'kind': 'conversation', 'id': 'meeting'},
        'candidates': list(candidates) or [candidate()],
    }


@pytest.fixture
def target(native_images):
    db, ai, secret = D1(), AI(), 'adjudication-screen-fixture-' * 3
    writer = Writer(db, secret)
    env = SimpleNamespace(
        APP_DB=db,
        AI=ai,
        IMAGES=native_images,
        SCREEN_FRAME_WRITER=writer,
        SCREEN_FRAME_SIGNING_SECRET=secret,
        INTERNAL_ASSERTION_SECRET='internal-test',
        PUBLIC_API_BASE_URL='https://api.eddy.test',
        SCREEN_FRAME_EGRESS_ENABLED='true',
    )
    app = FastAPI()
    app.include_router(router)
    app.include_router(views_router)

    @app.middleware('http')
    async def environment(request, call_next):
        request.scope['env'] = env
        return await call_next(request)

    async def call(payload=None, *, method='POST', path=PATH, uid='owner'):
        headers = {}
        if uid:
            encoded, signed = create_request_context(
                uid,
                env.INTERNAL_ASSERTION_SECRET,
                audience='api-core',
                method=method,
                path=urlsplit(path).path,
                request_id='adjudication-test',
                authority='better-auth',
            )
            headers = {'x-omi-auth-context': encoded, 'x-omi-internal-signature': signed}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=env.PUBLIC_API_BASE_URL) as client:
            return await client.request(method, path, headers=headers, json=payload)

    for uid in ('owner', 'other'):
        db.connection.execute(
            "INSERT INTO cf_conversations (uid, id, created_at, status, started_at, finished_at) "
            "VALUES (?, 'meeting', 1, 'completed', 1788656400, 1788656410)",
            (uid,),
        )
    yield SimpleNamespace(
        db=db,
        env=env,
        ai=ai,
        writer=writer,
        call=lambda *args, **kwargs: asyncio.run(call(*args, **kwargs)),
        async_call=call,
    )
    db.connection.close()


def state(target):
    return target.db.connection.execute("SELECT * FROM cf_screen_frame_sets WHERE uid = 'owner'").fetchone()


def test_image_service_failure_cannot_authorize_storage(target):
    target.env.IMAGES.failure = RuntimeError('native image service unavailable')
    response = target.call(body(candidate()))
    assert response.status_code == 200
    assert response.json()['outcome'] == 'no_approved_frames'
    assert target.ai.calls == [] and target.writer.calls == []


def test_approved_pipeline_judges_and_writes_identical_canonical_bytes_and_replays_once(target):
    request = body()
    result = target.call(request)
    assert result.status_code == 200, result.text
    value = result.json()
    assert value['outcome'] == 'committed' and value['frame_set']['revision'] == 1
    assert value['frame_set']['adjudicated_at']
    claims, jpeg, thumbnail = target.writer.calls[0]
    model, payload = target.ai.calls[0]
    assert model == 'google/gemini-2.5-flash-lite'
    assert payload['contents'][0]['parts'][0]['text'] == _PRIVACY_PROMPT
    assert base64.b64decode(payload['contents'][0]['parts'][1]['inlineData']['data']) == jpeg
    assert Image.open(io.BytesIO(jpeg)).size == (1600, 900)
    assert Image.open(io.BytesIO(thumbnail)).size == (480, 270)
    assert (claims['policy_version'], claims['prompt_version'], claims['decision']) == (
        'meeting_note_privacy.v1',
        'meeting_note_frame_judge.v1',
        'approved_clean',
    )
    assert 'approval' not in result.text and 'canonical_sha256' not in result.text
    assert target.call(request).json() == value
    assert len(target.ai.calls) == len(target.writer.calls) == 1
    usage = target.db.connection.execute('SELECT * FROM cf_llm_usage_daily').fetchone()
    assert (usage['feature'], usage['input_tokens'], usage['output_tokens'], usage['call_count']) == (
        'screen_frame_judge',
        120,
        30,
        1,
    )
    request['candidates'][0]['captured_at'] = '2026-09-06T01:00:01Z'
    assert target.call(request).status_code == 409


@pytest.mark.parametrize(
    'fault', ['flag', 'signer', 'writer', 'ai', 'unfinished', 'setting', 'window', 'owner', 'auth']
)
def test_admission_failures_never_call_the_model_or_stamp_an_attempt(target, fault):
    request, kwargs = body(), {}
    expected = 409
    if fault == 'flag':
        target.env.SCREEN_FRAME_EGRESS_ENABLED = 'false'
    elif fault == 'signer':
        target.env.SCREEN_FRAME_SIGNING_SECRET = 'short'
    elif fault == 'writer':
        target.writer.ready = 503
    elif fault == 'ai':
        target.env.AI = None
    elif fault == 'unfinished':
        target.db.connection.execute("UPDATE cf_conversations SET status='processing' WHERE uid='owner'")
    elif fault == 'setting':
        target.db.connection.execute("INSERT INTO cf_screen_frame_settings VALUES ('owner', 0)")
    elif fault == 'window':
        request['candidates'][0]['captured_at'] = '2026-09-06T00:00:00Z'
        expected = 400
    elif fault == 'owner':
        kwargs['uid'] = 'missing'
        expected = 404
    elif fault == 'auth':
        kwargs['uid'] = None
        expected = 401
    response = target.call(request, **kwargs)
    assert response.status_code == expected, response.text
    assert not target.ai.calls and not target.writer.calls and state(target) is None


def test_every_transport_digest_is_checked_before_any_model_call(target):
    request = body(candidate(), candidate(1))
    request['candidates'][1]['sha256_base64'] = base64.b64encode(b'wrong' * 6 + b'xx').decode()
    response = target.call(request)
    assert response.status_code == 400 and response.json()['detail']['code'] == 'digest_mismatch'
    assert not target.ai.calls and not target.writer.calls and state(target) is None


@pytest.mark.parametrize(
    'verdict',
    [
        {**APPROVED, 'outcome': 'rejected', 'reject_reason': 'credentials'},
        {**APPROVED, 'reject_reason': 'credentials'},
        {**APPROVED, 'outcome': 'rejected'},
        {**APPROVED, 'extra': True},
        RuntimeError('controlled provider unavailable'),
    ],
)
def test_rejected_malformed_or_failed_judge_never_writes_and_records_the_attempt(target, verdict):
    target.ai.results = [verdict]
    response = target.call(body())
    assert response.status_code == 200, response.text
    assert response.json()['outcome'] == 'no_approved_frames'
    assert response.json()['frame_set']['revision'] == 0 and response.json()['frame_set']['adjudicated_at']
    assert not target.writer.calls
    assert target.db.connection.execute('SELECT count(*) FROM cf_screen_frame_writes').fetchone()[0] == 0


def test_bad_codec_is_candidate_local_and_long_metadata_uses_original_normalization(target):
    target.ai.results = [{**APPROVED, 'caption': 'a' * 200, 'labels': list('abcdefghijk')}]
    response = target.call(body(candidate(raw=b'not an image'), candidate(1)))
    assert response.status_code == 200, response.text
    assert len(target.ai.calls) == len(target.writer.calls) == 1
    banner = response.json()['frame_set']['banner']
    assert len(banner['caption']) == 160 and len(banner['labels']) == 8


def test_cap_eviction_and_all_low_scoring_frames_keep_original_banner_strip_policy(target):
    target.ai.results = [{**APPROVED, 'banner_suitability': 0.2} for _ in range(8)]
    response = target.call(body(*[candidate(n) for n in range(8)]))
    assert response.status_code == 200, response.text
    value = response.json()['frame_set']
    assert value['banner'] is None and len(value['strip']) == 6
    assert len(json.loads(state(target)['frames_json'])) == 7
    phases = target.db.connection.execute('SELECT phase,count(*) FROM cf_screen_frame_writes GROUP BY phase').fetchall()
    assert dict(phases) == {'cleanup': 1, 'committed': 7}


@pytest.mark.parametrize('privacy', ['disable', 'delete'])
def test_privacy_change_while_judging_never_mints_or_writes(target, privacy):
    def revoke():
        if privacy == 'disable':
            target.db.connection.execute("INSERT INTO cf_screen_frame_settings VALUES ('owner', 0)")
        else:
            target.db.connection.execute("DELETE FROM cf_conversations WHERE uid='owner'")

    target.ai.hook = revoke
    result = target.call(body())
    assert result.status_code == 409 and not target.writer.calls
    assert not state(target) or state(target)['adjudicated_at'] is None


def test_writer_failure_returns_503_without_a_successful_replay_or_timestamp(target):
    target.writer.status = 503
    request = body()
    response = target.call(request)
    assert response.status_code == 503 and response.json()['detail']['code'] == 'writer_unavailable'
    assert state(target)['adjudicated_at'] is None
    assert target.call(request).json()['detail']['code'] == 'adjudication_in_progress_retry'
    assert len(target.ai.calls) == 1


def test_expired_ready_receipt_rolls_back_both_the_response_and_frame_set(target):
    def expire(statements):
        if not statements[0].sql.startswith('UPDATE cf_screen_frame_attempts'):
            target.db.before_batch = expire
            return
        target.db.connection.execute("UPDATE cf_screen_frame_writes SET phase='cleanup' WHERE uid='owner'")

    target.db.before_batch = expire
    response = target.call(body())
    assert response.status_code == 503
    assert state(target)['adjudicated_at'] is None and state(target)['frames_json'] == '[]'
    assert target.db.connection.execute('SELECT response_json FROM cf_screen_frame_attempts').fetchone()[0] is None


def test_publication_retries_a_competing_revision_without_losing_existing_frames(target):
    first = target.call(body())
    assert first.status_code == 200, first.text
    original = first.json()['frame_set']['banner']['id']
    raced = []

    async def compete(statements):
        if not statements[0].sql.startswith('UPDATE cf_screen_frame_attempts'):
            target.db.before_batch = compete
            return
        competing = await target.async_call(body(candidate(2)))
        assert competing.status_code == 200, competing.text
        raced.append(competing.json()['frame_set']['banner']['id'])

    target.db.before_batch = compete
    response = target.call(body(candidate(1)))
    assert response.status_code == 200, response.text
    assert raced and response.json()['frame_set']['revision'] == 3
    ids = [f['id'] for f in json.loads(state(target)['frames_json'])]
    assert len(ids) == 3 and original in ids and raced[0] in ids


@pytest.mark.parametrize('privacy', ['disable', 'delete', 'account'])
def test_privacy_change_after_writer_completion_prevents_atomic_publication(target, privacy):
    def revoke(statements):
        if not statements[0].sql.startswith('UPDATE cf_screen_frame_attempts'):
            target.db.before_batch = revoke
            return
        if privacy == 'disable':
            target.db.connection.execute("INSERT INTO cf_screen_frame_settings VALUES ('owner', 0)")
        elif privacy == 'delete':
            target.db.connection.execute("DELETE FROM cf_conversations WHERE uid='owner'")
        else:
            target.db.connection.execute("INSERT INTO cf_account_deletion_tombstones VALUES ('owner',1,9999999999)")

    target.db.before_batch = revoke
    response = target.call(body())
    assert response.status_code == 409, response.text
    assert len(target.writer.calls) == 1
    assert not state(target) or state(target)['adjudicated_at'] is None
    assert target.db.connection.execute('SELECT phase FROM cf_screen_frame_writes').fetchone()[0] == 'cleanup'
    assert (
        target.db.connection.execute(
            'SELECT count(*) FROM cf_screen_frame_attempts WHERE response_json IS NOT NULL'
        ).fetchone()[0]
        == 0
    )
