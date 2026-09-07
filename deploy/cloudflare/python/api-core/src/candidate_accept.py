"""Accept original task/workstream Candidates through one guarded D1 batch."""

from datetime import datetime, timezone

from candidate_create import parse_record, stored_record
from candidate_db import CandidateSnapshotChanged, CandidateTransaction, account_generation
from candidate_kernel_models import CandidateAction, CandidateStatus, CandidateSubjectKind
import candidate_kernel_policy as policy
import candidate_kernel_workflow as workflow
from candidate_resolve import resolution_receipt
from vector_search import vector_outbox_statement
from candidate_integrations import schedule_after_accept


async def validate_relationships(tx, *, goal_id, workstream_id, allow_ended_goal=False):
    """Mirror the upstream final relationship check inside the write snapshot."""
    if goal_id is not None:
        goal = await tx.row('goals', goal_id)
        if goal is None:
            raise policy.CandidateConflictError('goal does not exist')
        if goal['account_generation'] != tx.generation:
            raise policy.CandidateConflictError('goal account generation mismatch')
        if not allow_ended_goal and goal['status'] in {'achieved', 'abandoned'}:
            raise policy.CandidateConflictError('ended goal cannot receive new task links')
    if workstream_id is not None:
        workstream = await tx.row('workstreams', workstream_id)
        if workstream is None:
            raise policy.CandidateConflictError('workstream does not exist')
        if workstream['account_generation'] != tx.generation:
            raise policy.CandidateConflictError('workstream account generation mismatch')
        if workstream['goal_id'] != goal_id:
            raise policy.CandidateConflictError('task goal_id must match workstream goal_id')


def queue_index(tx, env, kind, key, when):
    tx.statements.append(
        vector_outbox_statement(
            env,
            uid=tx.uid,
            source_kind=kind,
            source_id=key,
            desired_version=int(when.timestamp()),
            operation='upsert',
        )
    )


async def pending_integration(tx, candidate, task_id, now, *, require_absent=False):
    existing = await tx.record('integrations', candidate.candidate_id)
    if require_absent and existing is not None:
        raise policy.CandidateConflictError('deterministic integration outbox id collision')
    tx.put(
        'integrations',
        candidate.candidate_id,
        {
            'outbox_id': candidate.candidate_id,
            'candidate_id': candidate.candidate_id,
            'task_id': task_id,
            'account_generation': candidate.account_generation,
            'status': 'pending',
            'attempt_count': 0,
            'created_at': now.isoformat(),
            'updated_at': now.isoformat(),
        },
    )


async def accept_workstream(tx, env, candidate, now):
    proposal = candidate.workstream_proposal
    if proposal is None:
        raise policy.CandidateConflictError('stored Candidate has no workstream proposal')
    await validate_relationships(tx, goal_id=candidate.goal_id, workstream_id=None)
    workstream_id = workflow._stable_id('workstream', tx.uid, tx.generation, candidate.candidate_id)
    task_id = policy.task_id_for_candidate(tx.uid, tx.generation, candidate.candidate_id)
    workstream = await tx.row('workstreams', workstream_id)
    task = await tx.task(task_id)
    if workstream is not None or task is not None:
        raise policy.CandidateConflictError('deterministic workstream resolution id collision')
    _, event_data = workflow._initial_event_storage(
        uid=tx.uid,
        workstream_id=workstream_id,
        source_key=candidate.candidate_id,
        summary='Work initiated from an accepted suggestion',
        evidence_refs=candidate.evidence_refs,
        now=now,
    )
    tx.insert_workstream(
        workstream_id,
        workflow._workstream_storage(
            workstream_id=workstream_id,
            title=proposal.title,
            objective=proposal.objective,
            goal_id=candidate.goal_id,
            summary=proposal.objective,
            now=now,
            account_generation=tx.generation,
        ),
        event_data,
    )
    anchor = proposal.anchor_task
    tx.insert_task(
        task_id,
        workflow._task_storage(
            task_id=task_id,
            description=anchor.description,
            goal_id=candidate.goal_id,
            workstream_id=workstream_id,
            source=candidate.source_surface,
            owner=anchor.owner,
            provenance=[ref.model_dump(mode='python') for ref in candidate.evidence_refs],
            due_at=anchor.due_at,
            due_confidence=anchor.due_confidence,
            priority=anchor.priority,
            recurrence_rule=anchor.recurrence_rule,
            recurrence_parent_id=anchor.recurrence_parent_id,
            now=now,
            account_generation=tx.generation,
        ),
    )
    await pending_integration(tx, candidate, task_id, now, require_absent=True)
    queue_index(tx, env, 'workstream', workstream_id, now)
    queue_index(tx, env, 'action_item', task_id, now)
    return {'result_task_id': task_id, 'result_workstream_id': workstream_id}


async def accept_task(tx, env, candidate, now, expected_task_links):
    if candidate.proposed_action == CandidateAction.create:
        task_id = policy.task_id_for_candidate(tx.uid, tx.generation, candidate.candidate_id)
        current = await tx.task(task_id)
        await validate_relationships(tx, goal_id=candidate.goal_id, workstream_id=candidate.workstream_id)
        if current is not None:
            if current.get('candidate_id') != candidate.candidate_id:
                raise policy.CandidateConflictError('deterministic task id collision')
        else:
            tx.insert_task(task_id, policy._task_create_storage(candidate, task_id=task_id, now=now))
            queue_index(tx, env, 'action_item', task_id, now)
        await pending_integration(tx, candidate, task_id, now)
    else:
        task_id = candidate.task_id
        current = await tx.task(task_id)
        if current is None:
            raise policy.CandidateNotFoundError(f'task:{task_id}')
        if current['account_generation'] not in {0, tx.generation}:
            raise policy.CandidateGenerationMismatchError('task account generation mismatch')
        old_links = (current.get('goal_id'), current.get('workstream_id'))
        if expected_task_links is not None and old_links != expected_task_links:
            raise policy.CandidateConflictError('task links changed while resolving Candidate')
        # D1 stores seconds. Advance the physical update/index revision when two
        # real changes occur in one second, as the ordinary task writer does.
        stored_now = datetime.fromtimestamp(
            max(int(now.timestamp()), int(current['updated_at'].timestamp()) + 1), timezone.utc
        )
        patch = policy._task_update_storage(candidate, current_task=current, now=stored_now)
        patch['account_generation'] = tx.generation
        goal_id = patch.get('goal_id', old_links[0])
        workstream_id = patch.get('workstream_id', old_links[1])
        await validate_relationships(
            tx, goal_id=goal_id, workstream_id=workstream_id, allow_ended_goal=(goal_id, workstream_id) == old_links
        )
        tx.patch_task(task_id, patch)
        queue_index(tx, env, 'action_item', task_id, stored_now)
    return {'result_task_id': task_id, 'expires_at': None}


async def _accept(env, uid, candidate_id, *, generation, expected_task_links, now):
    if await account_generation(env, uid) != generation:
        raise policy.CandidateGenerationMismatchError('account generation mismatch')
    tx = CandidateTransaction(env, uid, generation)
    candidate = parse_record(await tx.record('candidates', candidate_id))
    if candidate is None:
        raise policy.CandidateNotFoundError(candidate_id)
    if candidate.account_generation != generation:
        raise policy.CandidateGenerationMismatchError(candidate_id)
    if candidate.status == CandidateStatus.accepted:
        await tx.commit()
        return resolution_receipt(candidate, newly_resolved=False)
    if candidate.status != CandidateStatus.pending:
        raise policy.CandidateConflictError(f'Candidate already {candidate.status.value}')
    if candidate.subject_kind == CandidateSubjectKind.workstream:
        patch = await accept_workstream(tx, env, candidate, now)
    else:
        patch = await accept_task(tx, env, candidate, now, expected_task_links)
    resolved = candidate.model_copy(
        update={
            **patch,
            'status': CandidateStatus.accepted,
            'resolution_reason': 'accepted',
            'resolved_at': now,
        }
    )
    tx.put('candidates', candidate_id, stored_record(resolved))
    await tx.commit()
    return resolution_receipt(resolved, newly_resolved=True)


async def accept_candidate(env, uid, candidate_id, *, generation, expected_task_links=None, now=None):
    current = now or datetime.now(timezone.utc)
    for attempt in range(3):
        try:
            receipt = await _accept(
                env, uid, candidate_id, generation=generation, expected_task_links=expected_task_links, now=current
            )
            await schedule_after_accept(env, uid, generation, candidate_id)
            return receipt
        except CandidateSnapshotChanged:
            if attempt == 2:
                raise
