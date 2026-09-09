"""Candidate terminal decisions use real SQL rollback and concurrency admission."""

import asyncio
from datetime import datetime, timedelta, timezone
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from candidate_create import get_candidate
from candidate_kernel_models import CandidateStatus
from candidate_kernel_policy import CandidateConflictError, CandidateGenerationMismatchError, CandidateNotFoundError
from candidate_resolve import resolve_candidate_without_mutation
from test_candidate_create import create, env, proposal, rows


def resolve(env, candidate, *, status=CandidateStatus.rejected, reason=None, uid='owner', generation=0, now=None):
    return asyncio.run(
        resolve_candidate_without_mutation(
            env, uid, candidate.candidate_id, status=status, reason=reason, generation=generation, now=now
        )
    )


@pytest.mark.parametrize('workstream', [False, True])
@pytest.mark.parametrize('status', [CandidateStatus.rejected, CandidateStatus.expired])
def test_terminal_decision_replays_original_receipt_without_creating_tasks(env, workstream, status):
    candidate = create(env, proposal(workstream=workstream))
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    first = resolve(env, candidate, status=status, reason='user_choice', now=now)
    before = rows(env)
    repeated = resolve(env, candidate, status=status, reason='later_reason', now=now + timedelta(hours=1))
    assert first.newly_resolved and not repeated.newly_resolved
    assert first.model_copy(update={'newly_resolved': False}) == repeated
    assert rows(env) == before
    stored = asyncio.run(get_candidate(env, 'owner', candidate.candidate_id))
    assert stored.status == status and stored.expires_at is None
    assert stored.resolution_reason == 'user_choice' and stored.resolved_at == now
    assert env.APP_DB.connection.execute('SELECT count(*) FROM cf_action_items').fetchone()[0] == 0
    assert env.APP_DB.connection.execute('SELECT count(*) FROM cf_workstreams').fetchone()[0] == 0
    other_status = CandidateStatus.expired if status == CandidateStatus.rejected else CandidateStatus.rejected
    with pytest.raises(CandidateConflictError, match='already'):
        resolve(env, candidate, status=other_status)
    assert rows(env) == before


def test_resolution_is_scoped_to_account_and_generation(env):
    candidate = create(env)
    before = rows(env)
    with pytest.raises(CandidateNotFoundError):
        resolve(env, candidate, uid='other')
    with pytest.raises(CandidateGenerationMismatchError):
        resolve(env, candidate, generation=1)
    assert rows(env) == before


def test_late_resolution_failure_keeps_pending_record_and_clears_guard(env):
    candidate = create(env)
    before = rows(env)
    env.APP_DB.connection.execute(
        "CREATE TRIGGER fail_candidate_resolution BEFORE UPDATE ON cf_candidates "
        "BEGIN SELECT RAISE(ABORT,'resolution unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match='resolution unavailable'):
        resolve(env, candidate)
    assert rows(env) == before


@pytest.mark.parametrize('same_status', [False, True])
def test_competing_terminal_decision_is_observed_before_acknowledgement(env, same_status):
    candidate = create(env)
    winner_status = CandidateStatus.rejected if same_status else CandidateStatus.expired
    # The fake D1 connection is intentionally thread-affine. Run the competitor
    # on the same thread through a precomputed list of real prepared statements.
    # Its writes are captured, then committed at the outer before-write seam.
    original_batch = env.APP_DB.batch
    captured = []

    async def capture(statements):
        captured.extend(statements)

    env.APP_DB.batch = capture
    winner = resolve(env, candidate, status=winner_status)
    env.APP_DB.batch = original_batch

    def commit_competitor():
        connection = env.APP_DB.connection
        connection.execute('BEGIN')
        try:
            for statement in captured:
                statement.execute()
            connection.execute('COMMIT')
        except Exception:
            connection.execute('ROLLBACK')
            raise

    env.APP_DB.before_write = commit_competitor
    if same_status:
        replay = resolve(env, candidate)
        assert not replay.newly_resolved
        assert replay.receipt_id == winner.receipt_id and replay.resolved_at == winner.resolved_at
    else:
        with pytest.raises(CandidateConflictError, match='already expired'):
            resolve(env, candidate)
    stored = asyncio.run(get_candidate(env, 'owner', candidate.candidate_id))
    assert stored.status == winner_status and stored.resolved_at == winner.resolved_at
    assert rows(env)['cf_candidate_write_guard'] == []


@pytest.mark.parametrize('replay', [False, True])
@pytest.mark.parametrize('fence', ['generation', 'deletion'])
def test_generation_or_deletion_change_at_commit_never_acknowledges_stale_resolution(env, replay, fence):
    candidate = create(env)
    if replay:
        resolve(env, candidate)
    before = rows(env)

    def fence_account():
        connection = env.APP_DB.connection
        if fence == 'generation':
            connection.execute("INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',1,1)")
        else:
            connection.execute(
                "INSERT INTO cf_account_deletion_tombstones(uid,completed_at,expires_at) VALUES ('owner',1,100)"
            )

    env.APP_DB.before_write = fence_account
    with pytest.raises(CandidateGenerationMismatchError):
        resolve(env, candidate)
    assert rows(env) == before
