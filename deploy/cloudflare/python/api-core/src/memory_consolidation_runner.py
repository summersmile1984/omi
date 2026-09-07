"""One bounded leased batch, preserving upstream retry and terminal policy.

A durable dispatcher supplies ordered pending IDs and resumes remaining/delayed
work. This module does not register a public trigger or pretend to own scanning,
recurrence handoff, or the account maintenance watermark.
"""

from dataclasses import dataclass, field
from uuid import uuid4

from fallback import record_fallback
from memory_apply_intake import encoded, load_memory_control
from memory_apply_item import read_item
from memory_consolidation_apply import MAX_CONSOLIDATION_BATCH_ITEMS, apply_consolidation_batch
from memory_consolidation_leases import (
    ConsolidationLeaseChanged,
    claim_consolidation_attempt,
    instant,
    read_retry_state,
    release_consolidation_attempt,
)
from memory_consolidation_llm import ConsolidationInferenceError, consolidate_pending_with_llm
from memory_kernel_consolidation import (
    ConsolidationAgentBatch,
    ConsolidationApplySkipped,
    ConsolidationContext,
    MAX_CONSOLIDATION_FAILURE_ATTEMPTS,
    _is_promotable_for_consolidation,
    _safe_consolidation_failure_code,
    _terminal_review_decision,
)
from memory_vector_readiness import ConsolidationIndexPending


@dataclass
class ConsolidationBatchResult:
    selected: tuple = ()
    remaining: tuple = ()
    outcomes: dict = field(default_factory=dict)
    retry_at: dict = field(default_factory=dict)
    applied: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


def _error_code(error):
    if isinstance(error, (ConsolidationApplySkipped, ConsolidationInferenceError)):
        return _safe_consolidation_failure_code(str(error))
    return 'apply_blocked:' + type(error).__name__


def _deferred(error):
    return isinstance(error, (ConsolidationIndexPending, ConsolidationLeaseChanged)) or str(error) in {
        'input_too_large',
        'authority_changed',
        'memory_consolidation_authority_changed',
        'memory_consolidation_source_changed',
        'memory_consolidation_source_not_pending',
        'memory_consolidation_candidate_changed',
        'memory_consolidation_feedback_changed',
    }


def _telemetry(mode):
    record_fallback(
        component='other',
        from_mode='canonical_consolidation',
        to_mode='canonical_consolidation_' + mode,
        reason='other',
        outcome='degraded',
    )


async def _release(env, lease, result, error, *, deferred=False):
    key = lease.state.memory_id
    now = instant()
    try:
        state = await release_consolidation_attempt(env, lease, error=error, now=now, deferred=deferred)
    except ConsolidationLeaseChanged:
        result.outcomes[key] = 'ownership_changed'
        return None
    result.outcomes[key] = 'deferred' if deferred else 'retryable'
    result.retry_at[key] = int(now.timestamp()) + (5 if deferred else 0)
    return state


async def _settle_terminal(env, item, owner, run_id, result):
    key = item.memory_id
    lease, claimed = await claim_consolidation_attempt(env, item, owner=owner, terminal=True)
    if not claimed:
        result.outcomes[key] = 'busy'
        if lease and lease.state.lease_expires_at:
            result.retry_at[key] = int(lease.state.lease_expires_at.timestamp())
        return
    context = ConsolidationContext(uid=item.uid, pending_items=[item])
    batch = ConsolidationAgentBatch(decisions=[_terminal_review_decision(item)])
    for status in ('terminal_review', 'quarantined'):
        try:
            applied = await apply_consolidation_batch(
                env,
                context,
                batch,
                run_id=run_id + ':' + status,
                now=instant(),
                leases=(lease,),
                terminal_status=status,
            )
        except Exception as error:
            result.errors.append('terminal_' + status + ':' + type(error).__name__)
            if isinstance(error, ConsolidationLeaseChanged):
                result.outcomes[key] = 'ownership_changed'
                return
            continue
        result.applied.update(applied)
        result.outcomes[key] = status
        result.retry_at.pop(key, None)
        _telemetry('quarantine' if status == 'quarantined' else 'review')
        return
    await _release(env, lease, result, lease.state.last_error_code)
    _telemetry('retry')


async def run_consolidation_batch(env, uid, memory_ids, *, run_id):
    """Process at most one model batch; retry sources are isolated as upstream.

    Each invocation obtains a fresh lease identity even if the delivery/run ID
    repeats. An abandoned third attempt leases only terminal settlement and can
    never authorize a fourth model invocation. The result is a dispatcher handoff,
    not an acknowledgement of all supplied work.
    """
    ids = tuple(memory_ids)
    if (
        not isinstance(uid, str)
        or not uid.strip()
        or not isinstance(run_id, str)
        or not run_id.strip()
        or len(run_id) > 128
        or not 1 <= len(ids) <= MAX_CONSOLIDATION_BATCH_ITEMS
        or any(not isinstance(key, str) or not key.strip() for key in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError('invalid consolidation batch request')
    owner = str(uuid4())
    result = ConsolidationBatchResult()
    _, control = await load_memory_control(env, uid)
    rows = (
        await env.APP_DB.prepare('SELECT * FROM cf_memories WHERE uid=? AND id IN (SELECT value FROM json_each(?))')
        .bind(uid, encoded(ids))
        .all()
    )['results']
    items = {}
    for row in rows:
        try:
            items[row['id']] = read_item(row)
        except (ValueError, TypeError) as error:
            result.outcomes[row['id']] = 'unreadable_source'
            result.errors.append('source:read_' + type(error).__name__)
            _telemetry('retry')
    eligible = []
    for key in ids:
        if key in result.outcomes:
            continue
        item = items.get(key)
        if item is None or not _is_promotable_for_consolidation(item, now=instant()):
            result.outcomes[key] = 'not_pending'
        else:
            eligible.append(item)
    selected = []
    for item in eligible:
        try:
            prior = await read_retry_state(env, item, control)
        except (ValueError, TypeError) as error:
            # Preserve the malformed row and block the cycle watermark, while
            # allowing unrelated sources past it. Storage outages propagate.
            result.outcomes[item.memory_id] = 'unreadable_retry_state'
            result.errors.append('retry_state:read_' + type(error).__name__)
            _telemetry('retry')
            continue
        if prior and selected:
            break
        selected.append((item, prior))
        if prior:
            break
    result.selected = tuple(item.memory_id for item, _ in selected)
    result.remaining = tuple(
        item.memory_id
        for item in eligible
        if item.memory_id not in result.selected and item.memory_id not in result.outcomes
    )
    claims = []
    try:
        for item, prior in selected:
            key = item.memory_id
            if prior and prior[0].state.status in {'terminal_review', 'quarantined'}:
                result.outcomes[key] = prior[0].state.status
                continue
            if prior and prior[0].state.attempt_count >= MAX_CONSOLIDATION_FAILURE_ATTEMPTS:
                await _settle_terminal(env, item, owner, run_id, result)
                continue
            lease, claimed = await claim_consolidation_attempt(env, item, owner=owner)
            if claimed:
                claims.append(lease)
            else:
                result.outcomes[key] = 'busy'
                if lease:
                    due = prior[1] if prior else 0
                    expiry = lease.state.lease_expires_at
                    result.retry_at[key] = max(due, int(expiry.timestamp()) if expiry else 0)
    except Exception:
        for lease in claims:
            await _release(env, lease, result, 'retry_state:claim_failed', deferred=True)
        raise
    if not claims:
        return result
    try:
        result.applied.update(
            await consolidate_pending_with_llm(
                env,
                uid,
                [lease.state.memory_id for lease in claims],
                run_id=run_id,
                leases=tuple(claims),
            )
        )
    except Exception as error:
        code, deferred = _error_code(error), _deferred(error)
        result.errors.append(code)
        for lease in claims:
            state = await _release(env, lease, result, code, deferred=deferred)
            if state and not deferred and state.attempt_count >= MAX_CONSOLIDATION_FAILURE_ATTEMPTS:
                await _settle_terminal(env, items[lease.state.memory_id], owner, run_id, result)
        _telemetry('retry')
    else:
        for lease in claims:
            result.outcomes[lease.state.memory_id] = 'applied'
    return result
