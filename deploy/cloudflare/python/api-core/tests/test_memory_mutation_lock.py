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
        APP_DB=database,
        INTERNAL_ASSERTION_SECRET='mutation-test-secret',
        JOBS=SimpleNamespace(send=send),
        MEMORY_PRIVACY_SECRET='memory-privacy-tests-secret-32-bytes',
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
    expected = {}

    def lock():
        database.lock(memory_id)
        expected.update(row=database.row(memory_id), side_effects=database.side_effects())

    if lock_timing == 'before_read':
        lock()
    else:
        database.before_write = lock

    response = request(method, path.format(id=memory_id), body=body)

    assert response.status_code == 402, response.text
    key = 'detail' if path.startswith('/v1/') else 'error'
    assert response.json() == {key: 'A paid plan is required to access this memory.'}
    assert database.row(memory_id) == expected['row']
    assert database.side_effects() == expected['side_effects']


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
    expected = {}

    def lock():
        database.lock(locked_id)
        expected.update(rows={key: database.row(key) for key in (previous, candidate)}, effects=database.side_effects())

    if lock_timing == 'before_read':
        lock()
    else:
        database.before_write = lock
    body = {'decision': decision}
    if decision == 'correct':
        body['correction'] = {'content': 'Lives in LA', 'arg_changes': {'location': 'LA'}}
    response = request('POST', f"/v3/memories/review-queue/{item['review_id']}/resolve", body=body)
    assert response.status_code == 402, response.text
    for key, original in expected['rows'].items():
        assert database.row(key) == original
    assert database.side_effects() == expected['effects']


def test_same_second_edits_advance_canonical_revision_and_projection_work(target, monkeypatch):
    database, request, create = target
    monkeypatch.setattr(memory.time, 'time', lambda: 1_700_000_000)
    database.connection.create_function('unixepoch', 0, lambda: 1_700_000_000)
    memory_id = create()
    original = database.row(memory_id)
    for index in (1, 2):
        response = request('PATCH', f'/v3/memories/{memory_id}', body={'value': f'Revision {index}'})
        assert response.status_code == 200, response.text
        current = database.row(memory_id)
        assert current['updated_at'] == original['updated_at']
        assert current['item_revision'] == original['item_revision'] + index
        outbox = database.side_effects()['cf_vector_projection_outbox']
        assert len(outbox) == 1
        assert outbox[0]['desired_version'] == current['item_revision']
        # Upstream update_canonical_memory_content returns corrections to
        # pending Short-term; INV-MEM-4 requires re-admission before projection.
        assert current['processing_state'] == 'pending'
        assert outbox[0]['operation'] == 'delete'


def test_projection_failure_rolls_back_the_business_edit_and_revision(target):
    database, request, create = target
    memory_id = create()
    original, effects = database.row(memory_id), database.side_effects()
    database.connection.executescript('''
        CREATE TRIGGER unavailable_projection BEFORE UPDATE ON cf_vector_projection_outbox
        BEGIN SELECT RAISE(ABORT, 'private projection dependency failure'); END;
    ''')
    response = request('PATCH', f'/v3/memories/{memory_id}', body={'value': 'Must roll back'})
    assert response.status_code == 503
    assert response.json() == {'error': 'memories unavailable'}
    assert database.row(memory_id) == original
    assert database.side_effects() == effects


def test_lock_unlock_and_privacy_delete_coalesce_to_the_latest_revision(target):
    database, request, create = target
    memory_id = create()
    before = database.row(memory_id)['item_revision']
    database.lock(memory_id)
    assert database.side_effects()['cf_vector_projection_outbox'][0]['operation'] == 'delete'
    database.connection.execute('UPDATE cf_memories SET is_locked = 0 WHERE id = ?', (memory_id,))
    assert database.side_effects()['cf_vector_projection_outbox'][0]['operation'] == 'upsert'
    assert request('DELETE', f'/v3/memories/{memory_id}').status_code == 200
    outbox = database.side_effects()['cf_vector_projection_outbox'][0]
    assert outbox['operation'] == 'delete'
    assert outbox['desired_version'] == before + 3
    assert outbox['desired_version'] == database.row(memory_id)['item_revision']


def test_recreated_identity_supersedes_its_pending_hard_delete(target):
    database, request, create = target
    memory_id = create()
    database.connection.execute('DELETE FROM cf_memories WHERE id = ?', (memory_id,))
    deletion = database.side_effects()['cf_vector_projection_outbox'][0]
    assert deletion['operation'] == 'delete'
    database.connection.execute(
        "INSERT INTO cf_memories (uid, id, content, memory_tier, valid_at, created_at, updated_at) "
        "VALUES ('owner', ?, 'Recreated source', 'long_term', 100, 100, 100)",
        (memory_id,),
    )
    current = database.row(memory_id)
    outbox = database.side_effects()['cf_vector_projection_outbox'][0]
    assert current['item_revision'] > deletion['desired_version']
    assert outbox['desired_version'] == current['item_revision']
    assert outbox['operation'] == 'upsert'


def test_upgrade_rebases_timestamp_projections_and_preserves_account_deletion_fences():
    connection = sqlite3.connect(':memory:', isolation_level=None)
    connection.row_factory = sqlite3.Row
    directory = Path(__file__).parents[3] / 'migrations/app'
    migration = directory / '0162_memory_projection_revision.sql'
    try:
        for path in sorted(directory.glob('*.sql')):
            if path.name < migration.name:
                connection.executescript(path.read_text())
        for uid in ('owner', 'deleting'):
            connection.execute(
                "INSERT INTO cf_memories (uid, id, content, memory_tier, valid_at, created_at, updated_at) "
                "VALUES (?, 'old-memory', 'Legacy memory', 'long_term', 100, 100, 100)",
                (uid,),
            )
        connection.execute("UPDATE cf_memories SET is_locked = 1 WHERE uid = 'owner'")
        connection.execute(
            "INSERT INTO cf_vector_projection_state "
            "(uid, projection_kind, source_id, sub_id, vector_id, source_version, model, updated_at) "
            "VALUES ('owner', 'memory', 'old-memory', '000000', ?, 10040, 'prior-model', 100)",
            ('a' * 64,),
        )
        connection.execute(
            "INSERT INTO cf_vector_projection_outbox "
            "(uid, source_kind, source_id, desired_version, operation, next_attempt_at, created_at, updated_at) "
            "VALUES ('owner', 'memory', 'old-memory', 10090, 'upsert', 100, 100, 100)"
        )
        connection.execute(
            "INSERT INTO cf_account_deletion_intents "
            "(uid, job_id, status, phase, next_attempt_at, created_at, updated_at) "
            "VALUES ('deleting', 'deletion-job', 'running', 'purging', 100, 100, 100)"
        )
        frozen = dict(connection.execute("SELECT * FROM cf_memories WHERE uid = 'deleting'").fetchone())

        connection.executescript(migration.read_text())

        row = connection.execute("SELECT * FROM cf_memories WHERE uid = 'owner'").fetchone()
        pending = connection.execute("SELECT * FROM cf_vector_projection_outbox WHERE uid = 'owner'").fetchone()
        assert row['item_revision'] > 10090
        assert row['is_locked'] == 1
        assert pending['desired_version'] == row['item_revision']
        assert pending['operation'] == 'delete'
        assert dict(connection.execute("SELECT * FROM cf_memories WHERE uid = 'deleting'").fetchone()) == frozen
        assert connection.execute("SELECT * FROM cf_vector_projection_outbox WHERE uid = 'deleting'").fetchone() is None
        connection.execute("DELETE FROM cf_memories WHERE uid = 'deleting'")
        assert connection.execute("SELECT * FROM cf_vector_projection_outbox WHERE uid = 'deleting'").fetchone() is None
    finally:
        connection.close()
