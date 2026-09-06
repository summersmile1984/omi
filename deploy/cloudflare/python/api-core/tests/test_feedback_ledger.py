"""Rating HTTP routes, real migrations and atomic event/projection ownership."""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

import chat_routes
import chat_session_routes
import feedback_routes
import memory_routes
from feedback_contract import FeedbackEvent
from internal_auth import create_request_context
from user_export_routes import _rows


class Statement:
    def __init__(self, db, sql):
        self.db, self.sql, self.args = db, sql, ()

    def bind(self, *args):
        self.args = args
        return self

    def execute(self):
        self.db.queries.append(self.sql)
        if self.db.fail_sql and self.db.fail_sql in self.sql:
            raise RuntimeError('controlled D1 failure')
        return self.db.connection.execute(self.sql, self.args)

    async def first(self):
        row = self.execute().fetchone()
        return dict(row) if row else None

    async def all(self):
        return {'results': [dict(row) for row in self.execute().fetchall()]}

    async def run(self):
        cursor = self.execute()
        return {'meta': {'changes': cursor.rowcount}}


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:', isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys = ON')
        for path in sorted((Path(__file__).parents[3] / 'migrations/app').glob('*.sql')):
            self.connection.executescript(path.read_text())
        self.queries = []
        self.fail_sql = None
        self.before_batch = None

    def prepare(self, sql):
        return Statement(self, sql)

    async def batch(self, statements):
        if self.before_batch:
            callback, self.before_batch = self.before_batch, None
            callback()
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            result = [await statement.run() for statement in statements]
            self.connection.commit()
            return result
        except Exception:
            self.connection.rollback()
            raise


class Harness:
    def __init__(self):
        self.db = Database()
        self.env = SimpleNamespace(APP_DB=self.db, INTERNAL_ASSERTION_SECRET='feedback-test-secret')
        self.app = FastAPI()

        @self.app.middleware('http')
        async def environment(request, call_next):
            request.scope['env'] = self.env
            return await call_next(request)

        for router in (feedback_routes.router, chat_routes.router, chat_session_routes.router, memory_routes.router):
            self.app.include_router(router)

    async def request(self, method, path, uid='owner', body=None, headers=None, authority='better-auth'):
        supplied = dict(headers or {})
        if uid:
            encoded, signature = create_request_context(
                uid,
                self.env.INTERNAL_ASSERTION_SECRET,
                audience='api-core',
                method=method,
                path=path.split('?')[0],
                request_id='feedback-http-test',
                authority=authority,
            )
            supplied.update({'x-omi-auth-context': encoded, 'x-omi-internal-signature': signature})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://core') as client:
            return await client.request(method, path, json=body, headers=supplied)

    def message(self, uid='owner', message_id='answer', created_at=100, session_id='session', **extra):
        wire = {
            'id': message_id,
            'sender': 'ai',
            'text': 'PRIVATE TARGET TEXT',
            'created_at': datetime.fromtimestamp(created_at, timezone.utc).isoformat(),
            'chat_session_id': session_id,
            **extra,
        }
        self.db.connection.execute(
            'INSERT INTO cf_chat_messages (uid, id, app_id, created_at, message_json) VALUES (?, ?, ?, ?, ?)',
            (uid, message_id, 'assistant', created_at, json.dumps(wire)),
        )

    def events(self, uid='owner'):
        return [
            FeedbackEvent.model_validate_json(row[0])
            for row in self.db.connection.execute(
                'SELECT event_json FROM cf_feedback_events WHERE uid = ? ORDER BY created_at, id', (uid,)
            )
        ]


def test_real_http_records_every_surface_and_preserves_upstream_reasoned_vote():
    async def scenario():
        h = Harness()
        h.message(langsmith_run_id='run', prompt_name='unchanged-prompt', prompt_commit='commit')
        for surface in ('text', 'voice', 'notification'):
            result = await h.request(
                'PATCH',
                '/v2/desktop/messages/answer/rating',
                body={
                    'rating': -1,
                    'surface': surface,
                    'reason': 'not_useful',
                    'comment': '  why  ',
                    'app_version': '0.1.0',
                },
            )
            assert result.status_code == 200
        # An old client still needs only rating, and clearing appends a new event.
        assert (
            await h.request('PATCH', '/v2/desktop/messages/answer/rating', body={'rating': None})
        ).status_code == 200
        assert (
            await h.request(
                'PATCH',
                '/v2/messages/answer/rating',
                body={
                    'rating': -1,
                    'reason': 'other',
                    'comment': 'mobile reason',
                },
                headers={'X-App-Platform': ' Mobile '},
            )
        ).status_code == 200
        assert (
            await h.request('POST', '/v1/users/analytics/memory_summary?memory_id=conversation&value=-1')
        ).status_code == 200
        created = await h.request('POST', '/v3/memories', body={'content': 'PRIVATE MEMORY TEXT'})
        assert created.status_code == 200
        memory_id = created.json()['id']
        for value in ('false', 'true'):
            assert (await h.request('POST', f'/v3/memories/{memory_id}/review?value={value}')).status_code == 200
        events = h.events()
        assert [e.surface.value for e in events] == [
            'chat_text',
            'chat_voice',
            'chat_notification',
            'chat_text',
            'chat_text',
            'conversation_summary',
            'memory',
            'memory',
        ]
        assert [e.value for e in events] == [-1, -1, -1, 0, -1, -1, -1, 1]
        assert events[0].comment == 'why'
        assert events[0].chat_session_id == 'session'
        assert events[0].target_created_at.timestamp() == 100
        assert events[0].langsmith_run_id == 'run'
        assert events[0].prompt_name == 'unchanged-prompt'
        assert events[0].prompt_commit == 'commit'
        assert events[0].app_version == '0.1.0'
        assert events[3].reason is None and events[3].surface.value == 'chat_text'
        assert events[4].platform == 'mobile' and events[4].comment == 'mobile reason'
        assert 'PRIVATE' not in json.dumps([e.model_dump(mode='json') for e in events])
        assert len(await _rows(h.env, 'cf_feedback_events', 'created_at, id', 'owner')) == 8
        assert await _rows(h.env, 'cf_feedback_events', 'created_at, id', 'other') == []

    asyncio.run(scenario())


def test_legacy_unknown_reason_retains_vote_without_logging_user_text(capsys):
    async def scenario():
        h = Harness()
        h.message(uid='other')
        response = await h.request(
            'POST', '/v1/users/analytics/chat_message?message_id=answer&value=-1&reason=PRIVATE-UNKNOWN'
        )
        assert response.status_code == 200
        event = h.events()[0]
        assert event.reason is None and event.chat_session_id is None
        assert event.platform == 'mobile'
        assert h.events('other') == []

    asyncio.run(scenario())
    output = capsys.readouterr().out
    assert '"event":"fallback"' in output and 'PRIVATE' not in output


def test_feedback_rejects_invalid_modern_input_and_cross_account_desktop_target():
    async def scenario():
        h = Harness()
        h.message(uid='other')
        assert (await h.request('PATCH', '/v2/desktop/messages/answer/rating', body={'rating': -1})).status_code == 404
        h.message()
        for payload in (
            {'rating': -1, 'reason': 'invented'},
            {'rating': -1, 'comment': 'a' * 1001},
            {'rating': -1, 'surface': 'invented'},
            {'rating': 0},
        ):
            assert (await h.request('PATCH', '/v2/desktop/messages/answer/rating', body=payload)).status_code == 400
        assert (
            await h.request('PATCH', '/v2/desktop/messages/answer/rating', uid=None, body={'rating': -1})
        ).status_code == 401
        assert h.events() == []

    asyncio.run(scenario())


def test_event_failure_rolls_back_the_users_rating_and_memory_review():
    async def scenario():
        h = Harness()
        h.message()
        created = await h.request('POST', '/v3/memories', body={'content': 'Memory'})
        memory_id = created.json()['id']
        h.db.fail_sql = 'INSERT INTO cf_feedback_events'
        assert (await h.request('PATCH', '/v2/desktop/messages/answer/rating', body={'rating': -1})).status_code == 503
        assert (await h.request('POST', f'/v3/memories/{memory_id}/review?value=false')).status_code == 503
        h.db.fail_sql = None
        assert h.events() == []
        assert h.db.connection.execute('SELECT COUNT(*) FROM cf_user_feedback').fetchone()[0] == 0
        message = json.loads(h.db.connection.execute('SELECT message_json FROM cf_chat_messages').fetchone()[0])
        assert message.get('rating') is None
        assert h.db.connection.execute('SELECT reviewed FROM cf_memories WHERE id=?', (memory_id,)).fetchone()[0] == 0

    asyncio.run(scenario())


def test_late_memory_review_cannot_append_after_target_deletion():
    async def scenario():
        h = Harness()
        created = await h.request('POST', '/v3/memories', body={'content': 'Memory'})
        memory_id = created.json()['id']
        h.db.before_batch = lambda: h.db.connection.execute('DELETE FROM cf_memories WHERE id=?', (memory_id,))
        response = await h.request('POST', f'/v3/memories/{memory_id}/review?value=false')
        assert response.status_code == 200
        assert h.events() == []

    asyncio.run(scenario())


def test_event_identity_is_immutable_and_erasure_fences_future_writes():
    async def scenario():
        h = Harness()
        path = '/v1/users/analytics/chat_message?message_id=legacy&value=-1'
        assert (await h.request('POST', path)).status_code == 200
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            h.db.connection.execute("UPDATE cf_feedback_events SET uid='other'")
        h.db.connection.execute(
            "INSERT INTO cf_account_deletion_tombstones (uid, completed_at, expires_at) VALUES ('owner', 1, 9999999999)"
        )
        h.db.connection.execute("DELETE FROM cf_feedback_events WHERE uid='owner'")
        assert (await h.request('POST', path)).status_code == 503
        assert h.events() == []

    asyncio.run(scenario())
