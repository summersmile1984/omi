"""Memory search executes the complete D1 schema and production HTTP handlers."""

import asyncio
from types import SimpleNamespace
import pytest

from test_memory_mutation_lock import Database
from test_memory_routes import FakeRequest, FakeVectorAi, FakeVectorIndex, create, signed_headers
from memory_routes import search_vector_memory


def setup_memory():
    db = Database()
    env = SimpleNamespace(
        APP_DB=db,
        INTERNAL_ASSERTION_SECRET='memory-secret',
        MEMORY_PRIVACY_SECRET='memory-privacy-tests-secret-32-bytes',
        WORKERS_AI_VECTOR_MODEL='test-vector-model',
        AI=FakeVectorAi(),
        MEMORY_VECTORS=FakeVectorIndex(),
    )
    memory = create(env, 'memory-secret', content='Original coffee preference')
    return db, env, memory


def project(db, memory_id, vector_id, *, revision=None):
    row = db.row(memory_id)
    version = row['item_revision'] if revision is None else revision
    db.connection.execute(
        "INSERT INTO cf_vector_projection_state "
        "(uid, projection_kind, source_id, sub_id, vector_id, source_version, model, updated_at) "
        "VALUES ('memory-user', 'memory', ?, ?, ?, ?, 'test-vector-model', 1)",
        (memory_id, vector_id[:8], vector_id, version),
    )
    adopt_state(db)
    return version


def adopt_state(db):
    """The actual publication owner journals IDs before exposing mappings."""
    db.connection.execute(
        'INSERT OR IGNORE INTO cf_memory_vector_artifacts '
        '(vector_id, uid, source_id, attempt_id, sub_id, source_version, model, writer_until, writer_done) '
        "SELECT vector_id, uid, source_id, 'fixture:' || vector_id, sub_id, source_version, model, 0, 1 "
        "FROM cf_vector_projection_state WHERE projection_kind = 'memory'",
    )


def search(env, limit=10):
    return asyncio.run(
        search_vector_memory(
            FakeRequest(
                env,
                signed_headers('memory-secret'),
                {'query': 'coffee', 'limit': str(limit)},
            )
        )
    )


def test_stale_projection_does_not_return_current_memory_with_an_old_score():
    db, env, memory = setup_memory()
    vector_id = 'a' * 64
    project(db, memory['id'], vector_id, revision=0)
    env.MEMORY_VECTORS.matches = [{'id': vector_id, 'score': 0.99}]
    response = search(env)
    assert response['items'] == []
    assert response['hydration_rejected_stale_projection_count'] == 1
    assert response['repair_purge_candidate_count'] == 1
    assert (
        db.connection.execute(
            "SELECT COUNT(*) FROM cf_vector_projection_state WHERE projection_kind = 'memory'",
        ).fetchone()[0]
        == 0
    )


def test_content_and_revision_are_from_one_snapshot_when_a_write_follows_the_read():
    db, env, memory = setup_memory()
    vector_id = 'a' * 64
    version = project(db, memory['id'], vector_id)
    env.MEMORY_VECTORS.matches = [{'id': vector_id, 'score': 0.9}]
    original_prepare = db.prepare
    changed = False

    def prepare(sql):
        statement = original_prepare(sql)
        original_all = statement.all

        async def all_rows():
            nonlocal changed
            result = await original_all()
            if not changed:
                changed = True
                db.connection.execute(
                    "UPDATE cf_memories SET content = 'New preference after the snapshot' WHERE id = ?",
                    (memory['id'],),
                )
            return result

        statement.all = all_rows
        return statement

    db.prepare = prepare
    response = search(env)
    assert changed
    assert db.row(memory['id'])['content'] == 'New preference after the snapshot'
    assert response['items'][0]['content'] == 'Original coffee preference'
    assert response['projection_commit_ids_by_memory_id'] == {memory['id']: str(version)}


def test_overfetch_filters_before_limit_and_does_not_count_duplicates_as_missing():
    db, env, first = setup_memory()
    second = create(env, 'memory-secret', content='Second valid preference')
    locked = create(env, 'memory-secret', content='Locked private preference')
    for memory, vector_id in [(first, 'a' * 64), (first, 'b' * 64), (second, 'c' * 64), (locked, 'd' * 64)]:
        project(db, memory['id'], vector_id)
    db.connection.execute('UPDATE cf_memories SET is_locked = 1 WHERE id = ?', (locked['id'],))
    env.MEMORY_VECTORS.matches = [
        {'id': 'd' * 64, 'score': 0.99},
        {'id': 'a' * 64, 'score': 0.9},
        {'id': 'b' * 64, 'score': 0.89},
        {'id': 'e' * 64, 'score': 0.88},
        {'id': 'c' * 64, 'score': 0.8},
    ]
    response = search(env, limit=2)
    assert [row['id'] for row in response['items']] == [first['id'], second['id']]
    assert response['candidate_hydration_read_count'] == 5
    assert response['hydrated_candidate_count'] == 3
    assert response['hydration_rejected_missing_count'] == 1
    assert response['hydration_rejected_access_denied_count'] == 1
    assert response['repair_purge_candidates'] == []


@pytest.mark.parametrize(
    'mutation',
    [
        "memory_tier = 'archive'",
        'is_locked = 1',
        'user_review = 0',
        "sensitivity_labels_json = '[\"credential\"]'",
        'account_generation = 999',
        'expires_at = 1',
        "processing_state = 'pending'",
        "source_state = 'tombstoned'",
    ],
)
def test_current_canonical_access_denial_is_not_reported_as_stale_or_missing(mutation):
    db, env, memory = setup_memory()
    project(db, memory['id'], 'a' * 64)
    db.connection.execute(f'UPDATE cf_memories SET {mutation} WHERE id = ?', (memory['id'],))
    env.MEMORY_VECTORS.matches = [{'id': 'a' * 64, 'score': 0.9}]
    response = search(env)
    assert response['items'] == []
    assert response['hydration_rejected_access_denied_count'] == 1
    assert response['hydration_rejected_stale_projection_count'] == 0
    assert response['repair_purge_candidates'] == []


def test_repair_rechecks_canonical_revision_and_preserves_a_new_publication():
    db, env, memory = setup_memory()
    project(db, memory['id'], 'a' * 64, revision=0)
    env.MEMORY_VECTORS.matches = [{'id': 'a' * 64, 'score': 0.9}]

    def publish_newer():
        db.connection.execute(
            "UPDATE cf_memories SET content = 'New canonical preference' WHERE id = ?", (memory['id'],)
        )
        project(db, memory['id'], 'b' * 64)
        db.connection.execute("DELETE FROM cf_vector_projection_outbox WHERE source_kind = 'memory'")

    db.before_write = publish_newer
    response = search(env)
    assert response['items'] == []
    assert (
        db.connection.execute(
            "SELECT vector_id FROM cf_vector_projection_state WHERE projection_kind = 'memory'",
        ).fetchone()[0]
        == 'b' * 64
    )
    assert db.connection.execute('SELECT COUNT(*) FROM cf_vector_projection_outbox').fetchone()[0] == 0
    assert response['repair_purge_outbox_records'][0]['vector_id'] == 'a' * 64


def test_repair_failure_returns_service_unavailable_and_rolls_back_mapping_changes():
    db, env, memory = setup_memory()
    project(db, memory['id'], 'a' * 64, revision=0)
    env.MEMORY_VECTORS.matches = [{'id': 'a' * 64, 'score': 0.9}]
    db.connection.executescript(
        "CREATE TRIGGER fail_vector_repair BEFORE INSERT ON cf_vector_projection_outbox "
        "BEGIN SELECT RAISE(ABORT, 'private storage failure'); END;",
    )
    response = search(env)
    assert response.status_code == 503
    assert b'private storage failure' not in response.body
    assert db.connection.execute('SELECT COUNT(*) FROM cf_vector_projection_state').fetchone()[0] == 1


def test_foreign_provider_hit_cannot_reveal_or_repair_another_accounts_memory():
    db, env, memory = setup_memory()
    project(db, memory['id'], 'a' * 64)
    env.MEMORY_VECTORS.matches = [{'id': 'a' * 64, 'score': 0.9}]
    response = asyncio.run(
        search_vector_memory(
            FakeRequest(
                env,
                signed_headers('memory-secret', 'another-user'),
                {'query': 'coffee'},
            )
        )
    )
    assert response['items'] == []
    assert response['hydration_rejected_missing_count'] == 1
    assert response['repair_purge_outbox_records'] == []
    assert db.connection.execute('SELECT COUNT(*) FROM cf_vector_projection_state').fetchone()[0] == 1


def test_unpublished_old_attempt_is_stale_vector_while_the_current_mapping_remains_usable():
    db, env, memory = setup_memory()
    project(db, memory['id'], 'a' * 64)
    project(db, memory['id'], 'b' * 64)
    db.connection.execute("DELETE FROM cf_vector_projection_state WHERE vector_id = ?", ('a' * 64,))
    db.connection.execute('DELETE FROM cf_vector_projection_outbox')
    env.MEMORY_VECTORS.matches = [{'id': 'a' * 64, 'score': 0.95}, {'id': 'b' * 64, 'score': 0.9}]
    response = search(env)
    assert [row['id'] for row in response['items']] == [memory['id']]
    assert response['scores_by_memory_id'] == {memory['id']: 0.9}
    assert response['hydration_rejected_stale_vector_count'] == 1
    assert response['hydration_rejected_missing_count'] == 0
    assert response['decisions'][memory['id']] == 'allowed'
    assert response['repair_purge_outbox_records'][0]['vector_id'] == 'a' * 64
    assert db.connection.execute('SELECT COUNT(*) FROM cf_vector_projection_outbox').fetchone()[0] == 0


def test_missing_source_keeps_owned_vector_cleanup_and_recovers_lost_delete_work():
    db, env, memory = setup_memory()
    version = project(db, memory['id'], 'a' * 64)
    db.connection.execute('DELETE FROM cf_memories WHERE id = ?', (memory['id'],))
    db.connection.execute('DELETE FROM cf_vector_projection_outbox')
    env.MEMORY_VECTORS.matches = [{'id': 'a' * 64, 'score': 0.9}]
    response = search(env)
    assert response['items'] == []
    assert response['hydration_rejected_missing_count'] == 1
    assert response['repair_purge_candidate_count'] == 1
    assert response['repair_purge_outbox_record_count'] == 1
    work = dict(
        db.connection.execute(
            "SELECT * FROM cf_vector_projection_outbox WHERE source_kind = 'memory'",
        ).fetchone()
    )
    assert work['operation'] == 'delete'
    assert work['desired_version'] > version
