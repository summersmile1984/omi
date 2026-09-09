"""Original intervention/feedback policy on the existing D1 record owners."""

from datetime import datetime, timezone
import json

from candidate_create import create_candidate, get_candidate
from candidate_db import CandidateSnapshotChanged, CandidateTransaction, account_generation, encoded
from candidate_kernel_attention import _request_hash
from candidate_kernel_models import CandidateCreate, CandidateStatus, TaskCompleteCandidate
from candidate_kernel_recommendation import FeedbackCreate, FeedbackRecord, InterventionCreate, InterventionRecord
from candidate_kernel_suggested import _stable_id, DEFAULT_LATER_TTL, DISMISS_TTL
from candidate_kernel_policy import CandidateConflictError, CandidateGenerationMismatchError, CandidateNotFoundError
from candidate_resolve import resolve_candidate_without_mutation
from memory_kernel_action_item import EvidenceRef, EvidenceKind, EvidenceScope, TaskChangePayload, TaskStatus


def payload(row):
    return json.loads(row['payload_json'])


def intervention_record(row):
    value = payload(row)
    return InterventionRecord(
        **{key: value[key] for key in InterventionCreate.model_fields if key in value},
        intervention_id=row['intervention_id'],
        attribution_chain_id=row['attribution_chain_id'],
        created_at=value.get('created_at') or datetime.fromtimestamp(row['created_at'], timezone.utc),
    )


def feedback_record(row):
    value = payload(row)
    value = {key: value[key] for key in FeedbackRecord.model_fields if key in value}
    value.update(
        feedback_id=row['feedback_id'],
        attribution_chain_id=row['attribution_chain_id']
        or _stable_id('attr', row['uid'], row['account_generation'], value['subject_kind'], value['subject_id']),
        created_at=value.get('created_at') or datetime.fromtimestamp(row['created_at'], timezone.utc),
    )
    return FeedbackRecord.model_validate(value)


def same_request(kind, row, request):
    stored = payload(row)
    original = kind.model_validate({key: stored[key] for key in kind.model_fields if key in stored})
    return original == request


def physical_fingerprint(record_id, request):
    # Legacy physical UNIQUE(uid,generation,request_fingerprint) must not merge
    # distinct idempotency keys with identical bodies. The original value hash
    # remains in payload metadata; this index binds it to its record identity.
    return _request_hash({'record_id': record_id, 'request_hash': _request_hash(request.model_dump(mode='json'))})


async def transaction(env, uid, generation):
    if await account_generation(env, uid) != generation:
        raise CandidateGenerationMismatchError('account generation mismatch')
    return CandidateTransaction(env, uid, generation)


async def _intervention(env, uid, request, key, generation, now):
    tx = await transaction(env, uid, generation)
    identity = _stable_id('intervention', uid, generation, request.surface.value, key)
    existing = await tx.row('interventions', identity)
    if existing is not None:
        if existing['account_generation'] != generation or not same_request(InterventionCreate, existing, request):
            raise CandidateConflictError('idempotency key was used for a different intervention')
        await tx.commit()
        return intervention_record(existing)
    record = InterventionRecord(
        **request.model_dump(mode='python'),
        intervention_id=identity,
        attribution_chain_id=_stable_id('attr', uid, generation, identity),
        created_at=now,
    )
    data = record.model_dump(mode='json') | {'_request_hash': _request_hash(request.model_dump(mode='json'))}
    tx._insert(
        'cf_task_interventions',
        {
            'intervention_id': identity,
            'account_generation': generation,
            'attribution_chain_id': record.attribution_chain_id,
            'request_fingerprint': physical_fingerprint(identity, request),
            'payload_json': encoded(data),
            'created_at': now,
        },
    )
    await tx.commit()
    return record


async def register_intervention(env, uid, request: InterventionCreate, *, idempotency_key, generation, now=None):
    current = now or datetime.now(timezone.utc)
    if request.expires_at <= current:
        raise ValueError('intervention must expire in the future')
    for attempt in range(3):
        try:
            return await _intervention(env, uid, request, idempotency_key, generation, current)
        except CandidateSnapshotChanged:
            if attempt == 2:
                raise


async def _feedback(env, uid, request, key, generation, now, expiry):
    tx = await transaction(env, uid, generation)
    identity = _stable_id('feedback', uid, generation, key)
    attribution = _stable_id('attr', uid, generation, request.subject_kind.value, request.subject_id)
    dedupe_key = None
    if request.intervention_id is not None:
        intervention = await tx.row('interventions', request.intervention_id)
        if intervention is None or intervention['account_generation'] != generation:
            raise CandidateNotFoundError('intervention not found')
        context = payload(intervention)
        if (
            context.get('feedback_subject_kind', context.get('subject_kind')) != request.subject_kind.value
            or context.get('feedback_subject_id', context.get('subject_id')) != request.subject_id
        ):
            raise CandidateConflictError('feedback subject does not match intervention')
        attribution, dedupe_key = intervention['attribution_chain_id'], context['dedupe_key']
    existing = await tx.row('feedback', identity)
    if existing is not None:
        if existing['account_generation'] != generation or not same_request(FeedbackCreate, existing, request):
            raise CandidateConflictError('idempotency key was used for different feedback')
        record = feedback_record(existing)
        stored = payload(existing)
        # A pre-adapter feedback record is read through its original request and
        # timestamp. Repairing missing suppression never starts a fresh TTL.
        if '_override_expires_at' in stored:
            expiry = datetime.fromisoformat(stored['_override_expires_at']) if stored['_override_expires_at'] else None
        elif request.action.value == 'later':
            expiry = request.later_until or record.created_at + DEFAULT_LATER_TTL
        elif request.action.value == 'dismiss':
            expiry = record.created_at + DISMISS_TTL
        dedupe_key = record.dedupe_key or dedupe_key
    else:
        record = FeedbackRecord(
            **request.model_dump(mode='python'),
            feedback_id=identity,
            attribution_chain_id=attribution,
            created_at=now,
            dedupe_key=dedupe_key,
            proposed_completion=request.reason is not None and request.reason.value == 'already_handled',
        )
        stored = record.model_dump(mode='json') | {
            '_request_hash': _request_hash(request.model_dump(mode='json')),
            '_override_expires_at': expiry.isoformat() if expiry is not None else None,
        }
        tx._insert(
            'cf_task_feedback',
            {
                'feedback_id': identity,
                'account_generation': generation,
                'intervention_id': request.intervention_id,
                'attribution_chain_id': attribution,
                'request_fingerprint': physical_fingerprint(identity, request),
                'payload_json': encoded(stored),
                'created_at': now,
            },
        )
    if dedupe_key is not None and expiry is not None:
        override_id = _stable_id('override', uid, generation, dedupe_key)
        current_override = await tx.record('attention', override_id)
        if existing is None or current_override is None:
            tx.put(
                'attention',
                override_id,
                {
                    'override_id': override_id,
                    'account_generation': generation,
                    'dedupe_key': dedupe_key,
                    'intervention_id': request.intervention_id,
                    'feedback_id': identity,
                    'action': request.action.value,
                    'reason': request.reason.value if request.reason is not None else None,
                    'created_at': record.created_at.isoformat(),
                    'expires_at': expiry.isoformat(),
                },
            )
    await tx.commit()
    return record


async def _link_completion(env, uid, feedback_id, candidate_id, generation):
    tx = await transaction(env, uid, generation)
    row = await tx.row('feedback', feedback_id)
    if row is None or row['account_generation'] != generation:
        raise CandidateGenerationMismatchError('feedback generation mismatch')
    data = payload(row)
    data['proposed_completion_candidate_id'] = candidate_id
    tx.statements.append(
        tx.db.prepare('UPDATE cf_task_feedback SET payload_json=? WHERE uid=? AND feedback_id=?').bind(
            encoded(data), uid, feedback_id
        )
    )
    await tx.commit()
    return feedback_record({**row, 'payload_json': encoded(data)})


async def record_feedback(env, uid, request: FeedbackCreate, *, idempotency_key, generation, now=None):
    current = now or datetime.now(timezone.utc)
    expiry = None
    if request.action.value == 'later':
        expiry = request.later_until or current + DEFAULT_LATER_TTL
        if expiry <= current:
            raise ValueError('later_until must be in the future')
    elif request.action.value == 'dismiss':
        expiry = current + DISMISS_TTL
    for attempt in range(3):
        try:
            record = await _feedback(env, uid, request, idempotency_key, generation, current, expiry)
            break
        except CandidateSnapshotChanged:
            if attempt == 2:
                raise
    reason = request.reason.value if request.reason is not None else None
    if reason in {'already_handled', 'not_mine'} and request.subject_kind.value == 'candidate':
        candidate = await get_candidate(env, uid, request.subject_id)
        if candidate is not None and candidate.status == CandidateStatus.pending:
            await resolve_candidate_without_mutation(
                env,
                uid,
                candidate.candidate_id,
                status=CandidateStatus.rejected,
                reason=reason,
                generation=generation,
                now=current,
            )
    elif reason == 'already_handled' and request.subject_kind.value == 'task':
        candidate = await create_candidate(
            env,
            uid,
            CandidateCreate(
                root=TaskCompleteCandidate(
                    task_id=request.subject_id,
                    task_change=TaskChangePayload(status=TaskStatus.completed),
                    capture_confidence=1,
                    ownership_confidence=1,
                    evidence_refs=[
                        EvidenceRef(kind=EvidenceKind.external, id=record.feedback_id, scope=EvidenceScope.canonical)
                    ],
                    source_surface='feedback',
                )
            ),
            idempotency_key=f'already-handled:{record.feedback_id}',
            generation=generation,
            now=current,
        )
        for attempt in range(3):
            try:
                record = await _link_completion(env, uid, record.feedback_id, candidate.candidate_id, generation)
                break
            except CandidateSnapshotChanged:
                if attempt == 2:
                    raise
    return record
