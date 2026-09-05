"""Exercise every interactive writer against the current D1 row's lock.

SQLite executes the complete App migration chain and real route SQL. The
before-write seam models a lock acquired after a route has read the row; it
does not replace business handlers or the database admission decision.
"""

import asyncio
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

import developer_mutation_routes as developer  # noqa: E402
import mcp_routes as mcp  # noqa: E402
import memory_review_routes as review  # noqa: E402
import memory_routes as memory  # noqa: E402
from internal_auth import create_request_context, decode_context  # noqa: E402


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:', isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.before_write = None
        self.fail = False
        self.published = []
        for path in sorted((Path(__file__).parents[3] / 'migrations/app').glob('*.sql')):
            self.connection.executescript(path.read_text())

    def admit(self):
        if self.fail:
            raise RuntimeError('private storage detail')
        hook, self.before_write = self.before_write, None
        if hook:
            hook()

    def prepare(self, sql):
        database = self

        class Statement:
            args = ()

            def bind(self, *args):
                self.args = args
                return self

            def execute(self):
                if database.fail:
                    raise RuntimeError('private storage detail')
                return database.connection.execute(sql, self.args)

            async def first(self):
                row = self.execute().fetchone()
                return dict(row) if row else None

            async def all(self):
                return {'results': [dict(row) for row in self.execute().fetchall()]}

            async def run(self):
                database.admit()
                return {'meta': {'changes': self.execute().rowcount}}

        return Statement()

    async def batch(self, statements):
        self.admit()
        self.connection.execute('BEGIN')
        try:
            results = [{'meta': {'changes': statement.execute().rowcount}} for statement in statements]
            self.connection.execute('COMMIT')
            return results
        except Exception:
            self.connection.execute('ROLLBACK')
            raise

    def row(self, memory_id):
        row = self.connection.execute('SELECT * FROM cf_memories WHERE id = ?', (memory_id,)).fetchone()
        return dict(row) if row else None

    def lock(self, memory_id):
        self.connection.execute('UPDATE cf_memories SET is_locked = 1 WHERE id = ?', (memory_id,))

    def side_effects(self):
        return {
            'published': list(self.published),
            **{
                table: [dict(row) for row in self.connection.execute(f'SELECT * FROM {table}')]
                for table in ('cf_memory_review_queue', 'cf_vector_projection_outbox', 'cf_usage_sources')
            },
        }


@pytest.fixture
def target(monkeypatch):
    database = Database()

    async def send(message):
        database.published.append(message)

    env = SimpleNamespace(
        APP_DB=database, INTERNAL_ASSERTION_SECRET='mutation-test-secret', JOBS=SimpleNamespace(send=send)
    )
    app = FastAPI()
    # Specific review paths precede /v3/memories/{memory_id} as in composition.
    for router in (review.router, memory.router, developer.router, mcp.router):
        app.include_router(router)

    @app.middleware('http')
    async def environment(request, call_next):
        request.scope['env'] = env
        return await call_next(request)

    async def principal(request, required_scope):
        # This suite controls delegated authentication, not memory policy.
        expected = 'memories:write' if request.url.path.startswith('/v1/dev/') else 'memories.write'
        assert required_scope == expected
        context = decode_context(
            request.headers.get('x-omi-auth-context'),
            request.headers.get('x-omi-internal-signature'),
            env.INTERNAL_ASSERTION_SECRET,
        )
        assert context is not None
        return SimpleNamespace(uid=context['uid']), None

    monkeypatch.setattr(developer, '_authenticate', principal)
    monkeypatch.setattr(mcp, '_authenticate', principal)

    def request(method, path, *, body=None, uid='owner'):
        encoded, signature = create_request_context(
            uid,
            env.INTERNAL_ASSERTION_SECRET,
            audience='api-core',
            method=method,
            path=path.split('?')[0],
            request_id='mutation-test',
            authority='better-auth',
        )

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url='https://core.test'
            ) as client:
                return await client.request(
                    method,
                    path,
                    json=body,
                    headers={'x-omi-auth-context': encoded, 'x-omi-internal-signature': signature},
                )

        return asyncio.run(run())

    def create(**fields):
        response = request('POST', '/v3/memories', body={'content': 'Original', 'category': 'manual', **fields})
        assert response.status_code == 200, response.text
        return response.json()['id']

    yield database, request, create
    database.connection.close()


WRITERS = [
    ('PATCH', '/v3/memories/{id}', {'value': 'Updated'}, 'content', 'Updated'),
    ('PATCH', '/v3/memories/{id}/visibility', {'value': 'public'}, 'visibility', 'public'),
    ('POST', '/v3/memories/{id}/review?value=false', None, 'user_review', 0),
    ('PATCH', '/v3/memories/{id}/read', {'is_read': True, 'is_dismissed': True}, 'is_read', 1),
    ('PATCH', '/v3/memories/{id}/baseline?value=true', None, 'is_baseline', 1),
    ('PATCH', '/v1/mcp/memories/{id}?value=Updated', None, 'content', 'Updated'),
    ('PATCH', '/v1/dev/user/memories/{id}', {'content': 'Updated'}, 'content', 'Updated'),
]


@pytest.mark.parametrize('method,path,body,field,value', WRITERS)
@pytest.mark.parametrize('lock_timing', ['before_read', 'before_write'])
def test_locked_mutation_preserves_memory_and_transactional_side_effects(
    target, method, path, body, field, value, lock_timing
):
    database, request, create = target
    memory_id = create()
    if lock_timing == 'before_read':
        database.lock(memory_id)
    else:
        database.before_write = lambda: database.lock(memory_id)
    original, side_effects = database.row(memory_id), database.side_effects()

    response = request(method, path.format(id=memory_id), body=body)

    assert response.status_code == 402, response.text
    key = 'detail' if path.startswith('/v1/') else 'error'
    assert response.json() == {key: 'A paid plan is required to access this memory.'}
    assert database.row(memory_id) == {**original, 'is_locked': 1}
    assert database.side_effects() == side_effects


@pytest.mark.parametrize('method,path,body,field,value', WRITERS)
def test_unlocked_legacy_memory_can_mutate_only_under_its_owner(target, method, path, body, field, value):
    database, request, create = target
    memory_id = create()
    original = database.row(memory_id)
    response = request(method, path.format(id=memory_id), body=body, uid='other')
    assert response.status_code == 404, response.text
    assert database.row(memory_id) == original

    response = request(method, path.format(id=memory_id), body=body)
    assert response.status_code == 200, response.text
    assert database.row(memory_id)[field] == value
    if path.startswith('/v1/'):
        assert len(database.published) == 1
        assert database.published[0]['uid'] == 'owner'


@pytest.mark.parametrize('method,path,body,field,value', WRITERS)
def test_storage_failure_is_not_reported_as_a_paid_plan_denial(target, method, path, body, field, value):
    database, request, create = target
    memory_id = create()
    database.fail = True
    response = request(method, path.format(id=memory_id), body=body)
    assert response.status_code == 503
    assert response.json() == {'error': 'memories unavailable'}


def test_locked_memory_retains_privacy_delete_and_account_erasure(target):
    database, request, create = target
    memory_id = create()
    database.lock(memory_id)
    assert request('DELETE', f'/v3/memories/{memory_id}', uid='other').status_code == 404
    assert request('DELETE', f'/v3/memories/{memory_id}').status_code == 200
    assert database.row(memory_id)['deleted_at'] is not None
    assert database.row(memory_id)['is_locked'] == 1
    database.connection.execute('DELETE FROM cf_memories WHERE uid = ?', ('owner',))
    assert database.row(memory_id) is None


@pytest.mark.parametrize('decision', ['accept', 'reject', 'correct'])
@pytest.mark.parametrize('lock_timing', ['before_read', 'before_write'])
def test_review_resolution_rolls_back_all_memory_rows_and_receipt(target, decision, lock_timing):
    database, request, create = target
    common = {'category': 'system', 'predicate': 'resides_in', 'subject_entity_id': 'user'}
    previous = create(content='Lives in NYC', arguments={'location': 'NYC'}, veracity=0.9, **common)
    candidate = create(content='Lives in SF', arguments={'location': 'SF'}, veracity=0.4, **common)
    page = request('GET', '/v3/memories/review-queue')
    assert page.status_code == 200, page.text
    item = page.json()[0]
    assert item['fact_id'] == candidate and previous in item['conflict_with']
    # Accept mutates the candidate first; denial on the second row must roll it back.
    locked_id = previous if decision == 'accept' else candidate
    if lock_timing == 'before_read':
        database.lock(locked_id)
    else:
        database.before_write = lambda: database.lock(locked_id)
    originals = {key: database.row(key) for key in (previous, candidate)}
    effects = database.side_effects()
    body = {'decision': decision}
    if decision == 'correct':
        body['correction'] = {'content': 'Lives in LA', 'arg_changes': {'location': 'LA'}}
    response = request('POST', f"/v3/memories/review-queue/{item['review_id']}/resolve", body=body)
    assert response.status_code == 402, response.text
    for key, original in originals.items():
        assert database.row(key) == {**original, 'is_locked': int(key == locked_id)}
    assert database.side_effects() == effects
