"""Original outcome attribution policy with atomic, generation-scoped D1 receipts."""

from datetime import datetime, timezone
import json

from candidate_attention import physical_fingerprint, same_request, transaction
from candidate_db import CandidateSnapshotChanged, encoded
from candidate_kernel_attention import _request_hash
from candidate_kernel_policy import CandidateConflictError
from candidate_kernel_recommendation import OutcomeCreate, OutcomeRecord
from recommendation_kernel import _outcome_matches_chain, _stable_id


class AttributionChainNotFoundError(LookupError):
    pass


def outcome_record(row):
    payload = json.loads(row['payload_json'])
    data = {key: payload[key] for key in OutcomeRecord.model_fields if key in payload}
    data.update(
        outcome_id=row['outcome_id'],
        attribution_chain_id=row['attribution_chain_id'],
        occurred_at=data.get('occurred_at') or datetime.fromtimestamp(row['occurred_at'], timezone.utc),
    )
    return OutcomeRecord.model_validate(data)


class OutcomeSource:
    def __init__(self, tx):
        self.tx = tx

    async def candidate(self, identity):
        raw = await self.tx.record('candidates', identity)
        value = json.loads(raw) if raw else None
        return value if value and value.get('account_generation') == self.tx.generation else None

    async def task(self, identity):
        row = await self.tx.row('tasks', identity)
        return row if row and row['account_generation'] == self.tx.generation and not row['deleted'] else None

    async def artifact(self, workstream, identity):
        row = await self.tx.row('artifacts', identity)
        return (
            row
            if row and row['account_generation'] == self.tx.generation and row['workstream_id'] == workstream
            else None
        )


async def chain_source(tx, chain):
    for group, table, key in [
        ('interventions', 'cf_task_interventions', 'intervention_id'),
        ('feedback', 'cf_task_feedback', 'feedback_id'),
    ]:
        match = (
            await tx.db.prepare(
                f'SELECT {key} FROM {table} WHERE uid=? AND attribution_chain_id=? AND account_generation=? ORDER BY {key} LIMIT 1'
            )
            .bind(tx.uid, chain, tx.generation)
            .first()
        )
        if match is None:
            continue
        row = await tx.row(group, match[key])
        if not row or row['attribution_chain_id'] != chain or row['account_generation'] != tx.generation:
            raise CandidateSnapshotChanged('attribution source changed')
        return json.loads(row['payload_json'])
    raise AttributionChainNotFoundError(chain)


async def record_outcome(env, uid, generation, request: OutcomeCreate, *, idempotency_key, now=None):
    occurred_at = now or datetime.now(timezone.utc)
    identity = _stable_id('outcome', uid, generation, idempotency_key)
    for attempt in range(3):
        tx = await transaction(env, uid, generation)
        try:
            source = await chain_source(tx, request.attribution_chain_id)
            if not await _outcome_matches_chain(OutcomeSource(tx), request, source):
                raise CandidateConflictError('outcome subject or code does not match attribution chain')
            existing = await tx.row('outcomes', identity)
            created = existing is None
            if existing:
                if existing['account_generation'] != generation or not same_request(OutcomeCreate, existing, request):
                    raise CandidateConflictError('idempotency key was used for a different outcome')
                record = outcome_record(existing)
            else:
                record = OutcomeRecord(
                    **request.model_dump(mode='python'), outcome_id=identity, occurred_at=occurred_at
                )
                payload = record.model_dump(mode='json') | {
                    '_request_hash': _request_hash(request.model_dump(mode='json'))
                }
                tx._insert(
                    'cf_task_outcomes',
                    {
                        'outcome_id': identity,
                        'account_generation': generation,
                        'attribution_chain_id': request.attribution_chain_id,
                        'request_fingerprint': physical_fingerprint(identity, request),
                        'payload_json': encoded(payload),
                        'occurred_at': occurred_at,
                    },
                )
            await tx.commit()
            if created:
                print(
                    encoded(
                        {
                            'event': 'task_intelligence_attribution',
                            'kind': 'outcome',
                            'subject_kind': request.subject_kind.value,
                            'code': request.outcome_code.value,
                        }
                    )
                )
            return record
        except CandidateSnapshotChanged:
            if attempt == 2:
                raise
