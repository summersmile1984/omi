"""D1 ownership/CAS for the original non-content consolidation retry state."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from memory_apply_intake import load_memory_control
from memory_kernel_consolidation import (
    ConsolidationApplySkipped,
    ConsolidationRetryState,
    CONSOLIDATION_ATTEMPT_LEASE_SECONDS,
    MAX_CONSOLIDATION_FAILURE_ATTEMPTS,
    _is_promotable_for_consolidation,
    _safe_consolidation_failure_code,
)
from memory_kernel_contracts import deterministic_contract_id


class ConsolidationLeaseChanged(ConsolidationApplySkipped):
    pass


def instant(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('consolidation time must be aware')
    return datetime.fromtimestamp(int(value.timestamp()), timezone.utc)


def retry_id(item):
    return deterministic_contract_id(
        'canonical-consolidation-retry-state',
        {
            'uid': item.uid,
            'memory_id': item.memory_id,
            'source_item_revision': item.item_revision,
            'source_content_hash': item.content_hash,
        },
    )[:32]


@dataclass(frozen=True)
class ConsolidationLease:
    state: ConsolidationRetryState
    state_json: str
    account_generation: int
    source_generation: int
    retry_id: str

    def key(self):
        return (self.state.uid, self.retry_id, self.account_generation, self.source_generation)

    def claim(self):
        return {
            'retry_id': self.retry_id,
            'account_generation': self.account_generation,
            'source_generation': self.source_generation,
            'state_json': self.state_json,
        }

    def validate_source(self, item):
        if (
            self.state.uid != item.uid
            or self.state.memory_id != item.memory_id
            or self.state.source_item_revision != item.item_revision
            or self.state.source_content_hash != item.content_hash
        ):
            raise ConsolidationLeaseChanged('memory_consolidation_lease_source_changed')

    def validate(self, item, control):
        self.validate_source(item)
        if self.account_generation != control.account_generation or self.source_generation != control.source_generation:
            raise ConsolidationLeaseChanged('memory_consolidation_lease_generation_changed')


async def read_retry_state(env, item, control):
    row = (
        await env.APP_DB.prepare(
            'SELECT state_json, next_attempt_at, account_generation, source_generation FROM cf_memory_consolidation_attempts '
            'WHERE uid=? AND retry_id=?'
        )
        .bind(item.uid, retry_id(item))
        .first()
    )
    if row is None:
        return None
    state = ConsolidationRetryState.model_validate_json(row['state_json'])
    lease = ConsolidationLease(
        state, row['state_json'], row['account_generation'], row['source_generation'], retry_id(item)
    )
    lease.validate_source(item)
    return lease, row['next_attempt_at']


async def claim_consolidation_attempt(env, item, *, owner, now=None, terminal=False):
    now = instant(now)
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 256:
        raise ValueError('invalid consolidation lease owner')
    if not _is_promotable_for_consolidation(item, now=now):
        raise ConsolidationLeaseChanged('memory_consolidation_source_not_pending')
    prior_control, control = await load_memory_control(env, item.uid)
    if prior_control is None:
        raise ConsolidationLeaseChanged('memory_consolidation_control_not_materialized')
    read = await read_retry_state(env, item, control)
    previous, due = read if read else (None, 0)
    prior = previous.state if previous else None
    same_generation = previous is not None and (
        previous.account_generation == control.account_generation
        and previous.source_generation == control.source_generation
    )
    if prior:
        if prior.status in {'quarantined', 'terminal_review'} or due > int(now.timestamp()):
            return previous, False
        if (
            same_generation
            and prior.status == 'in_progress'
            and prior.lease_owner != owner
            and prior.lease_expires_at is not None
            and prior.lease_expires_at > now
        ):
            return previous, False
    exhausted = prior is not None and prior.attempt_count >= MAX_CONSOLIDATION_FAILURE_ATTEMPTS
    if terminal != exhausted:
        return previous, False
    if prior and same_generation and prior.status == 'in_progress' and prior.lease_owner == owner:
        return previous, True
    state = ConsolidationRetryState(
        uid=item.uid,
        memory_id=item.memory_id,
        source_item_revision=item.item_revision,
        source_content_hash=item.content_hash,
        attempt_count=prior.attempt_count if terminal else (prior.attempt_count if prior else 0) + 1,
        status='in_progress',
        last_error_code=prior.last_error_code if prior else 'attempt_claimed',
        last_attempt_at=now,
        lease_owner=owner,
        lease_expires_at=now + timedelta(seconds=CONSOLIDATION_ATTEMPT_LEASE_SECONDS),
    )
    payload = state.model_dump_json()
    claimed = (
        await env.APP_DB.prepare(
            'INSERT INTO cf_memory_consolidation_attempts '
            '(uid,retry_id,memory_id,source_item_revision,source_content_hash,account_generation,source_generation,'
            'state_json,lease_until,next_attempt_at) SELECT ?,?,?,?,?,?,?,?,?,? '
            'WHERE EXISTS (SELECT 1 FROM cf_memories WHERE uid=? AND id=? AND item_revision=? '
            "AND status='active' AND memory_tier='short_term' AND source_state='active' "
            'AND deleted_at IS NULL AND invalid_at IS NULL AND superseded_by IS NULL AND is_locked=0) '
            'AND EXISTS (SELECT 1 FROM cf_memory_apply_control WHERE uid=? AND control_json=?) '
            'ON CONFLICT(uid,retry_id) DO UPDATE SET '
            'state_json=excluded.state_json,lease_until=excluded.lease_until,next_attempt_at=excluded.next_attempt_at, '
            'account_generation=excluded.account_generation,source_generation=excluded.source_generation '
            'WHERE cf_memory_consolidation_attempts.state_json=?'
        )
        .bind(
            item.uid,
            retry_id(item),
            item.memory_id,
            item.item_revision,
            item.content_hash,
            control.account_generation,
            control.source_generation,
            payload,
            int(state.lease_expires_at.timestamp()),
            int(now.timestamp()),
            item.uid,
            item.memory_id,
            item.item_revision,
            item.uid,
            prior_control,
            previous.state_json if previous else None,
        )
        .run()
    )
    if claimed['meta']['changes'] != 1:
        return previous, False
    return (
        ConsolidationLease(state, payload, control.account_generation, control.source_generation, retry_id(item)),
        True,
    )


async def verify_consolidation_leases(env, leases, items, control):
    if not leases:
        return
    by_id = {item.memory_id: item for item in items}
    if len(leases) != len(by_id) or {lease.state.memory_id for lease in leases} != set(by_id):
        raise ConsolidationLeaseChanged('memory_consolidation_lease_partition')
    for lease in leases:
        lease.validate(by_id[lease.state.memory_id], control)
        valid = (
            await env.APP_DB.prepare(
                'SELECT 1 AS valid FROM cf_memory_consolidation_attempts '
                'WHERE uid=? AND retry_id=? AND account_generation=? AND source_generation=? '
                "AND state_json=? AND json_extract(state_json,'$.status')='in_progress' AND lease_until>unixepoch()"
            )
            .bind(*lease.key(), lease.state_json)
            .first()
        )
        if not valid:
            raise ConsolidationLeaseChanged('memory_consolidation_lease_changed')


def _finished(lease, status, error, now, *, deferred=False):
    return lease.state.model_copy(
        update={
            'attempt_count': max(lease.state.attempt_count - 1, 0) if deferred else lease.state.attempt_count,
            'status': status,
            'last_error_code': _safe_consolidation_failure_code(error),
            'last_attempt_at': now,
            'lease_owner': None,
            'lease_expires_at': None,
        }
    )


async def release_consolidation_attempt(env, lease, *, error, now=None, deferred=False):
    now = instant(now)
    state = _finished(lease, 'retryable', error, now, deferred=deferred)
    result = (
        await env.APP_DB.prepare(
            'UPDATE cf_memory_consolidation_attempts SET state_json=?,lease_until=NULL,next_attempt_at=? '
            'WHERE uid=? AND retry_id=? AND account_generation=? AND source_generation=? AND state_json=? '
            "AND lease_until>unixepoch() AND json_extract(state_json,'$.status')='in_progress' "
            'AND EXISTS (SELECT 1 FROM cf_memory_apply_control c WHERE c.uid=cf_memory_consolidation_attempts.uid '
            'AND c.account_generation=cf_memory_consolidation_attempts.account_generation '
            'AND c.source_generation=cf_memory_consolidation_attempts.source_generation)'
        )
        .bind(state.model_dump_json(), int(now.timestamp()) + (5 if deferred else 0), *lease.key(), lease.state_json)
        .run()
    )
    if result['meta']['changes'] != 1:
        raise ConsolidationLeaseChanged('memory_consolidation_lease_changed')
    return state


def settlement_statements(db, leases, *, terminal_status, now):
    statements = []
    for lease in leases:
        if terminal_status is None:
            statements.append(
                db.prepare(
                    'DELETE FROM cf_memory_consolidation_attempts WHERE uid=? AND retry_id=? '
                    'AND account_generation=? AND source_generation=? AND state_json=?'
                ).bind(*lease.key(), lease.state_json)
            )
        else:
            state = _finished(lease, terminal_status, lease.state.last_error_code, instant(now))
            statements.append(
                db.prepare(
                    'UPDATE cf_memory_consolidation_attempts SET state_json=?,lease_until=NULL,next_attempt_at=? '
                    'WHERE uid=? AND retry_id=? AND account_generation=? AND source_generation=? AND state_json=?'
                ).bind(state.model_dump_json(), int(now.timestamp()), *lease.key(), lease.state_json)
            )
    return statements
