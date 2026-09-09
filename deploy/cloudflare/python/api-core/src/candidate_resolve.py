"""Original Candidate terminal decisions committed against observed D1 state."""

from datetime import datetime, timezone

from candidate_create import parse_record, stored_record
from candidate_db import CandidateSnapshotChanged, CandidateTransaction, account_generation
from candidate_kernel_models import CandidateResolutionReceipt, CandidateStatus
import candidate_kernel_policy as policy


def resolution_receipt(candidate, *, newly_resolved):
    return CandidateResolutionReceipt(
        candidate_id=candidate.candidate_id,
        status=candidate.status,
        receipt_id=policy._stable_contract_id(
            'receipt', candidate.candidate_id, candidate.account_generation, candidate.status.value
        ),
        task_id=candidate.result_task_id,
        workstream_id=candidate.result_workstream_id,
        newly_resolved=newly_resolved,
        resolved_at=candidate.resolved_at,
    )


async def _resolve_without_mutation(env, uid, candidate_id, *, status, reason, generation, now):
    if await account_generation(env, uid) != generation:
        raise policy.CandidateGenerationMismatchError('account generation mismatch')
    tx = CandidateTransaction(env, uid, generation)
    candidate = parse_record(await tx.record('candidates', candidate_id))
    if candidate is None:
        raise policy.CandidateNotFoundError(candidate_id)
    if candidate.account_generation != generation:
        raise policy.CandidateGenerationMismatchError(candidate_id)
    if candidate.status == status:
        # Replays still observe the account and row fences at commit. They must
        # not acknowledge a stale success after account deletion or replacement.
        await tx.commit()
        return resolution_receipt(candidate, newly_resolved=False)
    if candidate.status != CandidateStatus.pending:
        raise policy.CandidateConflictError(f'Candidate already {candidate.status.value}')
    resolved = candidate.model_copy(
        update={
            'status': status,
            'resolution_reason': reason or status.value,
            'resolved_at': now,
            'expires_at': None,
        }
    )
    tx.put('candidates', candidate_id, stored_record(resolved))
    await tx.commit()
    return resolution_receipt(resolved, newly_resolved=True)


async def resolve_candidate_without_mutation(
    env,
    uid,
    candidate_id,
    *,
    status: CandidateStatus,
    reason,
    generation,
    now=None,
):
    """Reject or expire once; competing terminal decisions conflict on reread.

    Canonical D1 writers use one atomic commit, so no intermediate resolution
    lease is created. Historical staged records are not canonical Candidates.
    """
    if status not in {CandidateStatus.rejected, CandidateStatus.expired}:
        raise ValueError('status must be rejected or expired')
    current = now or datetime.now(timezone.utc)
    for attempt in range(3):
        try:
            return await _resolve_without_mutation(
                env,
                uid,
                candidate_id,
                status=status,
                reason=reason,
                generation=generation,
                now=current,
            )
        except CandidateSnapshotChanged:
            if attempt == 2:
                raise
