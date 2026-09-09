"""Durable original recurrence handoff and replay-safe Candidate consumption."""

from datetime import datetime, timezone
from types import SimpleNamespace

from candidate_attention import transaction
from candidate_create import create_candidate
from candidate_db import CandidateSnapshotChanged, account_generation
from candidate_kernel_association import RecurrenceInboxReceipt, RecurrenceInboxStatus
from candidate_kernel_policy import CandidateGenerationMismatchError, CandidateNotFoundError
from fallback import record_fallback
from recurrence_kernel import _receipt_id, consume_recurrence_signal


def now():
    return datetime.now(timezone.utc)


def parse(raw, generation):
    receipt = RecurrenceInboxReceipt.model_validate_json(raw) if raw is not None else None
    if receipt is not None and receipt.account_generation != generation:
        raise CandidateGenerationMismatchError('recurrence generation mismatch')
    return receipt


async def plan_handoff(env, uid, generation, signals, *, timestamp):
    """Freeze each first proposal; caller commits these statements with memory."""
    if not signals:
        return None, []
    tx = await transaction(env, uid, generation)
    pending, seen = [], set()
    for signal in signals:
        identity = _receipt_id(uid, signal.stable_loop_key, generation)
        if identity in seen:
            continue
        seen.add(identity)
        receipt = parse(await tx.record('recurrences', identity), generation)
        if receipt is None:
            receipt = RecurrenceInboxReceipt(
                receipt_id=identity,
                loop_key=signal.stable_loop_key,
                account_generation=generation,
                status=RecurrenceInboxStatus.pending,
                signal=signal,
                created_at=timestamp,
                updated_at=timestamp,
            )
            tx.put('recurrences', identity, receipt.model_dump(mode='json'))
        if receipt.status == RecurrenceInboxStatus.pending:
            pending.append(identity)
    return tx, pending


async def publish_hints(env, uid, receipts):
    for identity in receipts:
        try:
            await env.JOBS.send({'uid': uid, 'jobId': identity, 'kind': 'task_recurrence', 'payload': {}})
        except Exception:
            # The inbox is already durable. Cron rediscovers it independently.
            record_fallback(
                component='other',
                from_mode='recurrence_signal',
                to_mode='recurrence_inbox_retry',
                reason='enqueue_failed',
                outcome='degraded',
            )


class RecurrenceSource:
    def __init__(self, env):
        self.env = env

    async def control(self, uid):
        return SimpleNamespace(account_generation=await account_generation(self.env, uid))

    async def create_candidate(self, uid, proposal, *, idempotency_key, account_generation):
        return await create_candidate(
            self.env, uid, proposal, idempotency_key=idempotency_key, generation=account_generation
        )


async def settle(env, uid, generation, identity, *, outcome=None, error_code=None):
    for attempt in range(3):
        tx = await transaction(env, uid, generation)
        receipt = parse(await tx.record('recurrences', identity), generation)
        if receipt is None:
            raise CandidateNotFoundError(identity)
        if receipt.status == RecurrenceInboxStatus.completed:
            return receipt
        updated = receipt.model_copy(
            update={
                'status': RecurrenceInboxStatus.completed if outcome is not None else RecurrenceInboxStatus.pending,
                'attempts': receipt.attempts + 1,
                'last_outcome': outcome,
                'last_error_code': error_code,
                'updated_at': now(),
            }
        )
        tx.put('recurrences', identity, updated.model_dump(mode='json'))
        try:
            await tx.commit()
            return updated
        except CandidateSnapshotChanged:
            if attempt == 2:
                raise


async def process_receipt(env, uid, generation, identity):
    tx = await transaction(env, uid, generation)
    receipt = parse(await tx.record('recurrences', identity), generation)
    if receipt is None:
        raise CandidateNotFoundError(identity)
    await tx.commit()
    if receipt.status == RecurrenceInboxStatus.completed:
        return receipt
    try:
        result = await consume_recurrence_signal(
            RecurrenceSource(env), uid, receipt.signal, account_generation=generation
        )
        return await settle(env, uid, generation, identity, outcome=result.outcome)
    except CandidateGenerationMismatchError:
        raise
    except Exception as error:
        try:
            await settle(env, uid, generation, identity, error_code=type(error).__name__)
        finally:
            record_fallback(
                component='other',
                from_mode='recurrence_inbox',
                to_mode='recurrence_inbox_retry',
                reason='other',
                outcome='degraded',
            )
        raise
