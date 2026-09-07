"""Exercise the original receipt algorithm and D1 admission at real SQL writers.

Privacy apply/finalization is a separate owner. The controlled seal below seeds
its committed tombstone/receipt state, without claiming public DELETE coverage.
"""

import ast
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from memory_privacy_receipts import privacy_receipt_id
from developer_conversation_create_routes import _memory_insert
from test_memory_apply_intake import apply, database, rows, state  # noqa: F401
from test_memory_intake_tier import INTAKE_PATHS, category_io, payload  # noqa: F401
from test_memory_mutation_lock import target  # noqa: F401

SECRET = 'memory-privacy-tests-secret-32-bytes'
ENV = SimpleNamespace(MEMORY_PRIVACY_SECRET=SECRET)


def receipt(uid='owner', memory_id='deleted'):
    return privacy_receipt_id(ENV, uid, memory_id)


def seal(connection, uid='owner', memory_id='deleted', *, expired=False):
    """Persist the privacy owner's content-free state before testing creators."""
    key = receipt(uid, memory_id)
    connection.execute(
        "UPDATE cf_memories SET content = NULL, deleted_at = unixepoch(), status = 'tombstoned', "
        "source_state = 'tombstoned', privacy_receipt_id = ? WHERE uid = ? AND id = ?",
        (key, uid, memory_id),
    )
    age = 2592001 if expired else 0
    connection.execute(
        'INSERT INTO cf_memory_privacy_receipts VALUES (?, ?, unixepoch() - ?, unixepoch() - ? + 2592000)',
        (uid, key, age, age),
    )
    connection.commit()


def legacy(connection, uid='owner', memory_id='deleted'):
    connection.execute(
        "INSERT INTO cf_memories(uid, id, content, memory_tier, valid_at, created_at, updated_at) VALUES (?, ?, 'legacy', 'short_term', 1, 1, 1)",
        (uid, memory_id),
    )
    connection.commit()


@pytest.mark.parametrize('uid,memory_id', [('owner', 'deleted'), ('用户😀', '记忆-茶'), ('other', 'deleted')])
def test_hmac_matches_the_unchanged_upstream_function(monkeypatch, uid, memory_id):
    upstream = Path(__file__).resolve().parents[5] / 'backend/database/memory_apply_store.py'
    tree = ast.parse(upstream.read_text())
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'privacy_deletion_receipt_id'
    )
    namespace = dict(os=os, hmac=hmac, hashlib=hashlib, MemoryFirestoreApplyError=ValueError)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(upstream), 'exec'), namespace)
    monkeypatch.setenv('ENCRYPTION_SECRET', SECRET)
    assert receipt(uid, memory_id) == namespace['privacy_deletion_receipt_id'](uid, memory_id)
    assert len(receipt(uid, memory_id)) == 72
    fixtures = json.loads((Path(__file__).parents[3] / 'tests/fixtures/memory-privacy-receipts.json').read_text())
    example = next(value for value in fixtures if value['uid'] == uid)
    assert example['secret'] == SECRET and example['memory_id'] == memory_id
    assert receipt(uid, memory_id) == example['receipt_id']


@pytest.mark.parametrize('secret', [None, '', 'x' * 31, 123])
def test_missing_or_short_secret_cannot_create_an_unfenced_memory(database, secret):
    env = SimpleNamespace(APP_DB=database, MEMORY_PRIVACY_SECRET=secret)
    from memory_apply_intake import create_native_memories

    with pytest.raises(ValueError, match='secret is unavailable'):
        asyncio.run(create_native_memories(env, 'owner', rows(), [], source_surface='v3_batch'))
    assert all(not values for values in state(database).values())


def test_legacy_principal_and_unrelated_rows_remain_usable_but_erased_id_is_sealed(database):
    db = database.connection
    legacy(db)
    legacy(db, memory_id='retained')
    # A principal with no account cutover/control row is admitted as before.
    db.execute("UPDATE cf_memories SET content = 'editable' WHERE id = 'retained'")
    seal(db)
    db.execute("UPDATE cf_memories SET content = 'still editable' WHERE id = 'retained'")
    for sql, args in [
        ("UPDATE cf_memories SET content = 'late writer', deleted_at = NULL WHERE id = 'deleted'", ()),
        ("UPDATE cf_memories SET privacy_receipt_id = NULL WHERE id = 'deleted'", ()),
        ("UPDATE cf_memories SET id = 'different' WHERE id = 'deleted'", ()),
    ]:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, args)
    db.execute("DELETE FROM cf_memories WHERE uid = 'owner' AND id = 'deleted'")
    for key in (None, receipt()):
        with pytest.raises(sqlite3.IntegrityError, match='memory_privacy_deleted'):
            db.execute(
                "INSERT INTO cf_memories(uid,id,content,memory_tier,valid_at,created_at,updated_at,privacy_receipt_id) "
                "VALUES ('owner','deleted','replayed','short_term',1,1,1,?)",
                (key,),
            )
    # Same external ID is not a cross-account deletion, including legacy rows.
    legacy(db, uid='other')
    apply(database, [{**row, 'id': 'fresh-' + row['id']} for row in rows()])
    assert db.execute("SELECT content FROM cf_memories WHERE id = 'retained'").fetchone()[0] == 'still editable'


def test_blocked_creator_rolls_back_the_complete_batch(database):
    db = database.connection
    legacy(db)
    seal(db)
    db.execute("DELETE FROM cf_memories WHERE id = 'deleted'")
    db.commit()
    before = state(database)
    values = rows()
    values[1]['id'] = 'deleted'
    with pytest.raises(sqlite3.IntegrityError, match='memory_privacy_deleted'):
        apply(database, values)
    assert state(database) == before


@pytest.mark.parametrize('path', INTAKE_PATHS)
def test_public_creators_supply_receipts_after_an_existing_erasure(target, category_io, path):
    database, request, _ = target
    # A different erased memory must not prevent fresh creation in any family.
    legacy(database.connection)
    seal(database.connection)
    result = request('POST', path, body=payload(path))
    assert result.status_code == 200, result.text
    assert 'privacy_receipt' not in result.text
    row = database.connection.execute("SELECT * FROM cf_memories WHERE id != 'deleted'").fetchone()
    assert row['privacy_receipt_id'] == receipt(row['uid'], row['id'])


def test_mcp_content_id_replay_cannot_restore_a_physically_erased_memory(target, category_io):
    database, request, _ = target
    path = '/v1/mcp/memories'
    assert request('POST', path, body=payload(path)).status_code == 200
    row = database.connection.execute('SELECT * FROM cf_memories').fetchone()
    seal(database.connection, row['uid'], row['id'])
    database.connection.execute('DELETE FROM cf_memories WHERE uid = ? AND id = ?', (row['uid'], row['id']))
    database.connection.commit()
    before = state(database)
    denied = request('POST', path, body=payload(path))
    assert denied.status_code == 503
    assert 'memory_privacy' not in denied.text
    assert state(database) == before


def test_conversation_extraction_uses_the_same_receipt_gate(database):
    env = SimpleNamespace(APP_DB=database, MEMORY_PRIVACY_SECRET=SECRET)
    legacy(database.connection)
    seal(database.connection)
    values = [{**row, 'conversation_id': 'source'} for row in rows()]
    asyncio.run(database.batch([_memory_insert(env, values)]))
    assert database.connection.execute("SELECT privacy_receipt_id FROM cf_memories WHERE id = 'memory-0'").fetchone()[
        0
    ] == receipt('owner', 'memory-0')
    values = [{**values[0], 'id': 'deleted'}]
    with pytest.raises(sqlite3.IntegrityError, match='memory_privacy_deleted'):
        asyncio.run(database.batch([_memory_insert(env, values)]))


def test_receipt_requires_tombstone_has_fixed_ttl_and_expires(database):
    db = database.connection
    legacy(db)
    with pytest.raises(sqlite3.IntegrityError, match='memory_privacy_requires_tombstone'):
        db.execute('INSERT INTO cf_memory_privacy_receipts VALUES (?, ?, 1, 2592001)', ('owner', receipt()))
    seal(db, expired=True)
    with pytest.raises(sqlite3.IntegrityError, match='memory_privacy_receipt_immutable'):
        db.execute('UPDATE cf_memory_privacy_receipts SET expires_at = expires_at + 2592000')
    db.execute("DELETE FROM cf_memories WHERE id = 'deleted'")
    # Expired receipts no longer veto creation, even before the scheduled purge.
    legacy(db)


def test_upgrade_preserves_existing_rows_dependencies_and_active_content_constraints():
    db = sqlite3.connect(':memory:')
    try:
        directory = Path(__file__).parents[3] / 'migrations/app'
        migration = directory / '0174_memory_privacy_receipts.sql'
        for path in sorted(directory.glob('*.sql')):
            if path.name < migration.name:
                db.executescript(path.read_text())
        legacy(db, memory_id='retained')
        legacy(db, uid='other', memory_id='retained')
        db.execute("UPDATE cf_memories SET is_locked = 1 WHERE uid = 'other'")
        tables = ('cf_memories', 'cf_vector_projection_outbox', 'cf_memory_vector_artifacts')
        before = {table: db.execute(f'SELECT rowid, * FROM {table}').fetchall() for table in tables}
        dependencies = db.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE type IN ('index','view','trigger') "
            "AND sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        db.executescript(migration.read_text())
        # The only extra memory column is its initially-null opaque receipt key.
        assert [row[:-1] for row in db.execute('SELECT rowid, * FROM cf_memories')] == before['cf_memories']
        for table in tables[1:]:
            assert db.execute(f'SELECT rowid, * FROM {table}').fetchall() == before[table]
        after = set(
            db.execute(
                "SELECT type, name, sql FROM sqlite_schema WHERE type IN ('index','view','trigger') AND sql IS NOT NULL"
            )
        )
        assert set(dependencies) <= after
        for content in (None, '', 'x' * 50001):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("UPDATE cf_memories SET content = ? WHERE uid = 'owner'", (content,))
        with pytest.raises(sqlite3.IntegrityError, match='memory_locked_for_mutation'):
            db.execute("UPDATE cf_memories SET content = 'changed' WHERE uid = 'other'")
        seal(db, memory_id='retained')
        assert db.execute("SELECT content FROM cf_memories WHERE uid = 'owner'").fetchone()[0] is None
    finally:
        db.close()
