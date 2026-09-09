"""Read-only prompt dependencies and writable sources through actual D1 apply."""

import asyncio
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from memory_consolidation_context import gather_consolidation_context
from test_memory_consolidation_apply import decision, execute
from test_memory_consolidation_context import long_term, project, services
from test_memory_mutation_lock import target
from test_memory_review_routes import journal


def fixture(target, *, lock_feedback=False):
    db, request, create = target
    candidate = long_term(db, create, 'An existing preference')
    project(db, candidate, 'a' * 64)
    feedback = create(content='Never infer this fictional example is about me')
    assert request('POST', '/v3/memories/' + feedback + '/review?value=false').status_code == 200
    db.connection.execute(
        "UPDATE cf_memories SET status='hidden',is_locked=? WHERE id=?", (int(lock_feedback), feedback)
    )
    source = create(content='A new observation')
    env = services(db)
    env.MEMORY_VECTORS.matches = [{'id': 'a' * 64, 'score': 0.9}]
    context = asyncio.run(gather_consolidation_context(env, 'owner', [source]))
    assert [row.memory_id for row in context.owner_rejected_examples] == [feedback]
    assert [row.memory_id for row in context.candidates_by_anchor[source]] == [candidate]
    return db, context, candidate, feedback, source


def test_hidden_locked_feedback_and_candidate_can_be_read_without_being_mutated(target):
    db, context, candidate, feedback, source = fixture(target, lock_feedback=True)
    before = {key: db.row(key) for key in (candidate, feedback)}
    result = execute(db, context, decision(context.pending_items[0], 'reject'))
    assert set(result) == {source}
    assert result[source].promotion['route'] == 'reject'
    assert {key: db.row(key) for key in before} == before
    assert db.connection.execute('SELECT count(*) FROM cf_memory_apply_guard').fetchone()[0] == 0


@pytest.mark.parametrize('dependency', ['candidate', 'feedback'])
@pytest.mark.parametrize('change', ['content', 'lock', 'status', 'delete'])
def test_context_dependency_race_rejects_the_whole_write(target, dependency, change):
    db, context, candidate, feedback, source = fixture(target)
    key = candidate if dependency == 'candidate' else feedback
    captured = {}

    def race():
        if change == 'delete':
            db.connection.execute('DELETE FROM cf_memories WHERE id=?', (key,))
        else:
            assignment = {
                'content': "content='Changed after planning'",
                'lock': 'is_locked=1-is_locked',
                'status': "status=CASE status WHEN 'active' THEN 'hidden' ELSE 'active' END",
            }[change]
            db.connection.execute('UPDATE cf_memories SET ' + assignment + ' WHERE id=?', (key,))
        captured['journal'] = journal(db)

    db.before_write = race
    with pytest.raises(sqlite3.IntegrityError, match='memory_apply_observation_changed'):
        execute(db, context, decision(context.pending_items[0], 'reject'))
    assert journal(db) == captured['journal']
    assert db.row(source)['processing_state'] == 'pending'


def test_candidate_locked_after_retrieval_cannot_be_a_replacement_write_target(target):
    db, context, candidate, _, source = fixture(target)
    db.connection.execute('UPDATE cf_memories SET is_locked=1 WHERE id=?', (candidate,))
    before = journal(db)
    with pytest.raises(sqlite3.IntegrityError, match='memory_locked_for_mutation'):
        execute(
            db,
            context,
            decision(
                context.pending_items[0],
                'promote',
                reconciliation='replace',
                target_memory_id=candidate,
                supersedes=[candidate],
            ),
        )
    assert journal(db) == before
