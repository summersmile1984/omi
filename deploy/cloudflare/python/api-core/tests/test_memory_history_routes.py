"""Real ASGI/D1 history reads, using original admission and wire behavior."""

import ast
import __future__
import asyncio
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_candidate_entry import api
from test_candidate_routes import call
from test_jit_proactivity_reservations import env
import memory_history_store as storage
import memory_history_routes as routes
import memory_history_kernel as kernel
from memory_history_wire import memory_item_to_memorydb, memory_api_payload, MemoryApiExposure
from memory_apply_item import read_item
from memory_apply_intake import create_native_memories
from memory_routes import _batch_row, MemoryCreate

PATH = '/v3/memories/ledger-history'
ROOT = Path(__file__).resolve().parents[5]


def seed(api, env, *, identity=None, meta=None, **physical):
    env.APP_DB.connection.execute("INSERT OR IGNORE INTO cf_jit_flags VALUES ('owner',1,0,1)")
    key = identity or str(uuid.uuid4())
    row = _batch_row('owner', key, MemoryCreate(content='Previously lived in Shanghai.'), int(time.time()))
    asyncio.run(create_native_memories(env, 'owner', [row], [], source_surface='v3_api'))
    row = env.APP_DB.row(key)
    metadata = json.loads(row['canonical_metadata_json'])
    metadata.update(
        kind='fact',
        ledger_schema_version='knowledge_ledger.v1',
        intent_backed=True,
        write_reason='direct_user_statement',
        subject_scope='primary_user',
        slot='home_city',
        valid_to='2200-01-01T00:00:00+00:00',
        **(meta or {}),
    )
    updates = dict(
        status='superseded',
        processing_state='processed',
        memory_tier='long_term',
        canonical_metadata_json=json.dumps(metadata),
        **physical,
    )
    env.APP_DB.connection.execute(
        'UPDATE cf_memories SET ' + ','.join(k + '=?' for k in updates) + ' WHERE id=?', (*updates.values(), key)
    )
    return identity or key


def read(api, suffix='', **kwargs):
    return call(api, 'GET', PATH + suffix, **kwargs)


def original_page(rows, limit=100, offset=0):
    tree = ast.parse((ROOT / 'backend/utils/memory/memory_service.py').read_text())
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'MemoryService')
    method = next(
        node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == 'read_ledger_history_page'
    )
    namespace = dict(vars(kernel))
    namespace['iter_authoritative_product_memory_items_newest_first'] = lambda uid, **kw: iter(rows[: kw['limit']])
    exec(
        compile(
            ast.Module(body=[method], type_ignores=[]),
            'upstream-history',
            'exec',
            flags=__future__.annotations.compiler_flag,
        ),
        namespace,
    )
    service = SimpleNamespace(db_client=None, _is_ledger_history_item=kernel.is_ledger_history_item)
    return namespace['read_ledger_history_page'](service, 'owner', limit=limit, offset=offset)


def test_fresh_account_and_disabled_or_unknown_rollout_are_read_only(api, env, monkeypatch):
    async def should_not_scan(self, *args):
        raise AssertionError('disabled rollout scanned memory')

    monkeypatch.setattr(storage.HistoryStore, 'page', should_not_scan)
    assert read(api).json() == []
    assert read(api).headers['cache-control'] == 'no-store'
    assert read(api, uid=None).status_code == 401
    assert env.APP_DB.connection.execute('SELECT COUNT(*) FROM cf_memory_apply_control').fetchone()[0] == 0
    env.APP_DB.fail = True
    assert read(api).json() == []


def test_enabled_fresh_account_is_empty_without_creating_control(api, env):
    env.APP_DB.connection.execute("INSERT INTO cf_jit_flags VALUES ('owner',1,0,1)")
    assert read(api).json() == []
    assert env.APP_DB.connection.execute('SELECT COUNT(*) FROM cf_memory_apply_control').fetchone()[0] == 0


def test_original_wire_order_projection_and_pagination(api, env):
    ids = [seed(api, env, identity=name, updated_at=8000000000) for name in ['b', 'a', 'c']]
    items = [read_item(env.APP_DB.row(key)) for key in ids]
    expected = original_page(items)
    response = read(api, '?uid=another&account_generation=19')
    assert response.status_code == 200, response.text
    assert [r['id'] for r in response.json()] == ['a', 'b', 'c']
    assert response.json() == json.loads(
        json.dumps(
            [memory_api_payload(r, MemoryApiExposure.CANONICAL) for r in expected.memories],
            default=lambda v: v.isoformat(),
        )
    )
    row = response.json()[0]
    assert row['memory_id'] == row['id'] and row['ledger_status'] == 'superseded' and row['invalid_at']
    assert row['slot'] == 'home_city' and row['subject_scope'] == 'primary_user' and row['intent_backed']
    assert row['evidence'][0]['source_id'] and 'currency' not in row and 'canonical_metadata_json' not in row
    for limit, offset in [(1, 1), (500, 0), (-1, -2), (0, 0)]:
        expected = original_page(items, limit, offset)
        response = read(api, f'?limit={limit}&offset={offset}')
        assert [r['id'] for r in response.json()] == [r.id for r in expected.memories]
    assert read(api, '?limit=500&offset=4501').status_code == 413
    assert read(api, '?limit=invalid').status_code == 422
    assert read(api, uid='another').json() == []


@pytest.mark.parametrize(
    'case',
    [
        'hidden',
        'tombstoned',
        'purged',
        'sensitive',
        'locked',
        'metadata_locked',
        'rejected',
        'legacy',
        'current',
        'pending',
        'deleted',
    ],
)
def test_original_history_policy_and_physical_privacy(api, env, case):
    key = seed(api, env)
    row = env.APP_DB.row(key)
    meta = json.loads(row['canonical_metadata_json'])
    updates = {}
    if case in {'hidden', 'tombstoned'}:
        updates['status'] = case
    elif case == 'purged':
        updates['source_state'] = 'purged'
    elif case == 'sensitive':
        updates['sensitivity_labels_json'] = '["secret"]'
    elif case == 'locked':
        updates['is_locked'] = 1
    elif case == 'metadata_locked':
        meta['promotion']['is_locked'] = True
    elif case == 'pending':
        updates.update(processing_state='pending', memory_tier='short_term')
    elif case == 'deleted':
        updates['deleted_at'] = 1
    elif case == 'rejected':
        updates.update(status='active', user_review=0)
        meta['valid_to'] = None
    elif case == 'legacy':
        updates['status'] = 'active'
        meta.update(valid_to=None, intent_backed=False, write_reason='legacy_migration')
    elif case == 'current':
        updates['status'] = 'active'
        meta['valid_to'] = None
    updates['canonical_metadata_json'] = json.dumps(meta)
    env.APP_DB.connection.execute(
        'UPDATE cf_memories SET ' + ','.join(k + '=?' for k in updates) + ' WHERE id=?', (*updates.values(), key)
    )
    response = read(api)
    assert response.status_code == 200, response.text
    assert [r['id'] for r in response.json()] == ([key] if case in {'rejected', 'legacy'} else [])
    default = call(api, 'GET', '/v3/memories')
    if case in {'hidden', 'tombstoned', 'purged', 'sensitive', 'deleted', 'rejected'}:
        assert key not in [r['id'] for r in default.json()]


@pytest.mark.parametrize('change', ['lock', 'content', 'delete', 'generation', 'kill', 'new_row'])
def test_change_during_read_never_returns_stale_content(api, env, monkeypatch, change):
    key = seed(api, env)
    original = storage.HistoryStore.items

    async def changed(self, limit, budget):
        async for item in original(self, limit, budget):
            yield item
        sql = {
            'lock': "UPDATE cf_memories SET is_locked=1 WHERE id=?",
            'content': "UPDATE cf_memories SET content='Concurrent change' WHERE id=?",
            'delete': "UPDATE cf_memories SET deleted_at=1 WHERE id=?",
            'generation': "UPDATE cf_memories SET account_generation=9 WHERE id=?",
            'kill': "UPDATE cf_jit_flags SET kill_switch=1 WHERE uid='owner'",
            'new_row': "UPDATE cf_memories SET updated_at=8000000001 WHERE id=?",
        }[change]
        env.APP_DB.connection.execute(sql, () if change == 'kill' else (key,))

    monkeypatch.setattr(storage.HistoryStore, 'items', changed)
    response = read(api)
    assert response.status_code == (200 if change == 'kill' else 503), response.text
    assert 'Shanghai' not in response.text


@pytest.mark.parametrize('failure', ['query', 'missing_control', 'malformed', 'generation', 'revoked_account'])
def test_unavailable_authority_is_not_successful_empty(api, env, monkeypatch, failure):
    key = seed(api, env)
    if failure == 'query':

        async def fail(*args):
            raise RuntimeError('private database content')

        monkeypatch.setattr(storage.HistoryStore, 'page', fail)
    elif failure == 'missing_control':
        env.APP_DB.connection.execute('DELETE FROM cf_memory_apply_control')
    elif failure == 'malformed':
        env.APP_DB.connection.execute("UPDATE cf_memories SET canonical_metadata_json='{}' WHERE id=?", (key,))
        env.APP_DB.connection.execute("UPDATE cf_memory_apply_control SET control_json='{}'")
    elif failure == 'generation':
        env.APP_DB.connection.execute('UPDATE cf_memories SET account_generation=9 WHERE id=?', (key,))
    else:
        env.APP_DB.connection.execute("INSERT INTO cf_account_deletion_tombstones VALUES ('owner',1,9999999999)")
    result = read(api)
    assert result.status_code == (404 if failure == 'revoked_account' else 503), result.text
    assert 'Shanghai' not in result.text and 'private database content' not in result.text


def test_501_provider_sentinel_and_byte_truncation_are_explicit(api, env, monkeypatch):
    key = seed(api, env)
    row = env.APP_DB.row(key)
    columns = [r['name'] for r in env.APP_DB.connection.execute('PRAGMA table_info(cf_memories)')]
    sql = 'INSERT INTO cf_memories(' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
    env.APP_DB.connection.executemany(
        sql, [tuple({**row, 'id': f'history-{i:04}'}[c] for c in columns) for i in range(499)]
    )
    full = read(api, '?limit=500')
    assert len(full.json()) == 500 and 'x-omi-list-truncated' not in full.headers
    env.APP_DB.connection.execute(sql, tuple({**row, 'id': 'history-last'}[c] for c in columns))
    partial = read(api, '?limit=500')
    assert len(partial.json()) == 500 and partial.headers['x-omi-list-truncated'] == 'true'
    monkeypatch.setattr(storage, 'SCAN_BYTES', 10_000)
    bounded = read(api, '?limit=500')
    assert 0 < len(bounded.json()) < 500 and bounded.headers['x-omi-list-truncated'] == 'true'


def test_budget_exhaustion_preserves_truncation_not_storage_errors(api, env, monkeypatch):
    seed(api, env)
    monkeypatch.setenv('OMI_LIST_READ_BUDGET_SECONDS', '0')
    response = read(api)
    assert response.status_code == 200 and response.json() == [] and response.headers['x-omi-list-truncated'] == 'true'


def test_large_canonical_metadata_obeys_real_transfer_and_scan_bounds(api, env, monkeypatch):
    key = seed(api, env)
    row = env.APP_DB.row(key)
    metadata = json.loads(row['canonical_metadata_json'])
    metadata['promotion']['audit_fixture'] = 'x' * 150_000
    env.APP_DB.connection.execute(
        'UPDATE cf_memories SET canonical_metadata_json=? WHERE id=?', (json.dumps(metadata), key)
    )
    row = env.APP_DB.row(key)
    columns = [r['name'] for r in env.APP_DB.connection.execute('PRAGMA table_info(cf_memories)')]
    sql = 'INSERT INTO cf_memories(' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
    env.APP_DB.connection.executemany(
        sql, [tuple({**row, 'id': 'large-history-' + str(i)}[c] for c in columns) for i in range(40)]
    )
    original = storage.HistoryStore.page
    transfers = []

    async def measured(self, cursor, limit):
        result = await original(self, cursor, limit)
        transfers.append(sum(len(value['document'].encode()) for value in result))
        return result

    monkeypatch.setattr(storage.HistoryStore, 'page', measured)
    response = read(api, '?limit=500')
    assert response.status_code == 200, response.text
    assert 1 < len(response.json()) < 41 and response.headers['x-omi-list-truncated'] == 'true'
    assert max(transfers) <= storage.PAGE_BYTES
    assert 'audit_fixture' not in response.text
