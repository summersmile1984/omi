"""Original Candidate idempotency/coalescing policy committed through D1 CAS."""

from datetime import datetime, timezone
import json

import candidate_kernel_policy as policy
from candidate_kernel_models import CandidateCreate, CandidateRecord, CandidateStatus
from memory_kernel_action_item import TaskChangePayload, TaskCreatePayload
from candidate_db import CandidateTransaction, CandidateSnapshotChanged, account_generation


def parse_record(raw):
    return CandidateRecord.model_validate_json(raw) if raw is not None else None


def request_fields(value):
    # TaskChangePayload uses field presence: due_at=null clears a date, while
    # omission leaves it unchanged. The upstream value hash alone omits this
    # distinction, so the alias also records non-content field names.
    return sorted(value.task_change.model_fields_set) if isinstance(value.task_change, TaskChangePayload) else None


def stored_record(value):
    payload = value.model_dump(mode='json', exclude_none=True)
    if isinstance(value.task_change, TaskChangePayload):
        payload['task_change'] = value.task_change.model_dump(mode='json', exclude_unset=True)
    return payload


async def get_candidate(env, uid, candidate_id):
    row = (
        await env.APP_DB.prepare('SELECT record_json FROM cf_candidates WHERE uid=? AND candidate_id=?')
        .bind(uid, candidate_id)
        .first()
    )
    return parse_record(row['record_json']) if row else None


async def _create(env, uid, proposal, idempotency_key, generation, now):
    if await account_generation(env, uid) != generation:
        raise policy.CandidateGenerationMismatchError('account generation mismatch')
    key_hash = policy._stable_contract_id('idem', uid, generation, idempotency_key)
    request_hash = policy._proposal_request_hash(uid, generation, proposal)
    candidate_id = policy.candidate_id_for_idempotency(uid, generation, idempotency_key)
    semantic_id = policy._pending_semantic_claim_id(uid, generation, proposal)
    tx = CandidateTransaction(env, uid, generation)
    alias_raw = await tx.record('aliases', key_hash)
    if alias_raw is not None:
        alias = json.loads(alias_raw)
        if (
            alias.get('account_generation') != generation
            or alias.get('request_hash') != request_hash
            or alias.get('request_fields') != request_fields(proposal)
        ):
            raise policy.CandidateConflictError('idempotency key was already used for a different proposal')
        key = alias.get('candidate_id')
        if not isinstance(key, str) or not key:
            raise policy.CandidateConflictError('candidate idempotency alias is malformed')
        existing = parse_record(await tx.record('candidates', key))
        if existing is None or existing.account_generation != generation:
            raise policy.CandidateConflictError('candidate idempotency alias target is missing or stale')
        await tx.commit()
        return existing

    existing = parse_record(await tx.record('candidates', candidate_id))
    alias = {
        'candidate_id': candidate_id,
        'request_hash': request_hash,
        'account_generation': generation,
        'created_at': now.isoformat(),
        'request_fields': request_fields(proposal),
    }
    if existing is not None:
        if (
            existing.account_generation != generation
            or existing.idempotency_key != key_hash
            or existing.as_proposal() != proposal
            or request_fields(existing) != request_fields(proposal)
        ):
            raise policy.CandidateConflictError('idempotency key was already used for a different proposal')
        tx.put('aliases', key_hash, alias)
        await tx.commit()
        return existing

    claimed = None
    task = None
    if semantic_id is not None:
        raw = await tx.record('claims', semantic_id)
        claim = json.loads(raw) if raw is not None else {}
        key = claim.get('candidate_id')
        if claim.get('account_generation') == generation and isinstance(key, str):
            claimed = parse_record(await tx.record('candidates', key))
            if claimed is not None and claimed.account_generation != generation:
                claimed = None
            if claimed is not None and claimed.status == CandidateStatus.accepted and claimed.result_task_id:
                task = await tx.task(claimed.result_task_id)
    active_accept = bool(
        claimed is not None
        and claimed.status == CandidateStatus.accepted
        and task is not None
        and policy._accepted_task_is_active(task, account_generation=generation)
        and policy._accepted_task_matches_semantic_claim(task, claimed)
    )
    pending = bool(
        claimed is not None
        and claimed.status == CandidateStatus.pending
        and claimed.created_at.tzinfo is not None
        and not policy.candidate_has_lapsed(claimed, now=now)
    )
    if claimed is not None and (pending or active_accept):
        record = policy._merge_candidate_annotations(claimed, proposal)
        alias['candidate_id'] = record.candidate_id
        if active_accept:
            patch = {
                'capture_confidence': max(
                    policy._stored_confidence(task.get('capture_confidence')), record.capture_confidence
                ),
                'ownership_confidence': max(
                    policy._stored_confidence(task.get('ownership_confidence')), record.ownership_confidence
                ),
                'provenance': policy._merge_task_provenance(task, record.evidence_refs),
                'updated_at': now,
            }
            if isinstance(record.task_change, TaskCreatePayload):
                confidence = policy._max_optional_confidence(
                    policy._stored_optional_confidence(task.get('due_confidence')), record.task_change.due_confidence
                )
                priority = policy._strongest_task_priority(
                    policy._stored_task_priority(task.get('priority')), record.task_change.priority
                )
                if confidence is not None:
                    patch['due_confidence'] = confidence
                if priority is not None:
                    patch['priority'] = priority.value
            tx.patch_task(record.result_task_id, patch)
    else:
        payload = proposal.model_dump(mode='python')
        if isinstance(proposal.task_change, TaskChangePayload):
            payload['task_change'] = proposal.task_change
        record = CandidateRecord(
            **payload,
            candidate_id=candidate_id,
            account_generation=generation,
            idempotency_key=key_hash,
            created_at=now,
            expires_at=now + policy.SUGGESTION_TTL
        )
    tx.put('candidates', record.candidate_id, stored_record(record))
    tx.put('aliases', key_hash, alias)
    if semantic_id is not None:
        tx.put(
            'claims',
            semantic_id,
            {
                'candidate_id': record.candidate_id,
                'account_generation': generation,
                'semantic_version': policy.PENDING_CANDIDATE_SEMANTIC_VERSION,
                'last_seen_at': now.isoformat(),
            },
        )
    await tx.commit()
    return record


async def create_candidate(env, uid, proposal: CandidateCreate, *, idempotency_key, generation, now=None):
    if not idempotency_key.strip() or generation < 0:
        raise ValueError('invalid Candidate identity')
    current = now or datetime.now(timezone.utc)
    for attempt in range(3):
        try:
            return await _create(env, uid, proposal, idempotency_key, generation, current)
        except CandidateSnapshotChanged:
            if attempt == 2:
                raise
