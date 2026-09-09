"""D1 snapshots and atomic receipts for the original JIT reservation policy."""

from datetime import datetime, timezone
import json

from candidate_db import CandidateTransaction, CandidateSnapshotChanged, account_generation
from candidate_kernel_policy import CandidateGenerationMismatchError
from jit_authority import resolve
from jit_proactivity_kernel import (
    propose_receipt,
    reserve_policy,
    JITProactivityReservationError,
    JITMalformedAuthority,
    _timezone_from_user_snapshot,
)
from memory_apply_item import read_item

TIMEZONE_QUERY = 'SELECT time_zone FROM cf_user_fcm_tokens WHERE uid=? ORDER BY updated_at DESC,device_key DESC LIMIT 1'


def clock():
    return datetime.now(timezone.utc)


def wire(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [wire(item) for item in value]
    return value


class ReservationStore:
    def __init__(self, env, uid, generation, flags):
        self.env, self.uid, self.generation, self.flags = env, uid, generation, flags
        self.tx = CandidateTransaction(env, uid, generation)
        self.timezone_name, self.control_json, self.trigger_snapshot = None, None, None

    async def profile(self):
        row = await self.env.APP_DB.prepare(TIMEZONE_QUERY).bind(self.uid).first()
        self.timezone_name = row['time_zone'] if row else None
        return {'time_zone': self.timezone_name}

    async def control(self):
        row = (
            await self.env.APP_DB.prepare('SELECT control_json FROM cf_memory_apply_control WHERE uid=?')
            .bind(self.uid)
            .first()
        )
        self.control_json = row['control_json'] if row else None
        return json.loads(self.control_json) if self.control_json is not None else None

    async def trigger(self, memory_id):
        row = (
            await self.env.APP_DB.prepare('SELECT * FROM cf_memories WHERE uid=? AND id=?')
            .bind(self.uid, memory_id)
            .first()
        )
        if row is None:
            return None
        self.trigger_snapshot = [
            memory_id,
            row['account_generation'],
            row['item_revision'],
            row['canonical_metadata_json'],
        ]
        try:
            return read_item(row).model_dump(mode='python')
        except (ValueError, TypeError, KeyError) as error:
            raise JITMalformedAuthority('JIT trigger record is malformed') from error

    async def record(self, family, key):
        raw = await self.tx.record(family, key)
        value = json.loads(raw) if raw is not None else None
        if value is not None and family == 'jit_budget_controls':
            for field in ('window_ends_at', 'updated_at'):
                if isinstance(value.get(field), str):
                    value[field] = datetime.fromisoformat(value[field])
        return value

    def put(self, family, key, value):
        self.tx.put(family, key, wire(value))

    async def commit(self):
        db = self.env.APP_DB
        trigger = self.trigger_snapshot or [None, None, None, None]
        self.tx.statements.insert(
            0,
            db.prepare(
                'INSERT INTO cf_jit_reservation_guard(uid,flags_json,control_json,time_zone,trigger_id,trigger_generation,trigger_revision,trigger_metadata_json) VALUES (?,?,?,?,?,?,?,?)'
            ).bind(self.uid, self.flags, self.control_json, self.timezone_name, *trigger),
        )
        self.tx.statements.append(db.prepare('DELETE FROM cf_jit_reservation_guard WHERE uid=?').bind(self.uid))
        await self.tx.commit()


async def reserve(env, uid, request):
    proposed = None
    now = clock()
    for attempt in range(5):
        try:
            decision, generation, flags = await resolve(env, uid)
            if not decision.permits_work:
                return None
            if generation != request.account_generation or await account_generation(env, uid) != generation:
                raise JITProactivityReservationError('JIT generation is stale')
            store = ReservationStore(env, uid, generation, flags)
            if proposed is None:
                profile = await store.profile()
                proposed = propose_receipt(
                    uid, **request.model_dump(), timezone_name=_timezone_from_user_snapshot(profile), now=now
                )
            receipt, reserved = await reserve_policy(store, proposed)
            # Replays commit the read guards too; an old receipt cannot authorize
            # work after a timezone/trigger/parent/flag change during this read.
            await store.commit()
            return receipt, reserved
        except CandidateSnapshotChanged:
            if attempt == 4:
                raise JITProactivityReservationError('JIT reservation changed concurrently')
        except CandidateGenerationMismatchError as error:
            raise JITProactivityReservationError('JIT generation changed') from error
