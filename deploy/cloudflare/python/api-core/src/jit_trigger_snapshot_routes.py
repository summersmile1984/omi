"""Original desktop watchlist policy over current canonical D1 authority."""

import hashlib
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from candidate_routes import context
from jit_authority import resolve
from jit_trigger_snapshot_kernel import (
    MAX_AUTHORITATIVE_TRIGGERS,
    V3AccountGenerationFailureReason as Failure,
    V3TrustedAccountGenerationResult,
    read_authoritative_trigger_snapshot,
)
from jit_trigger_snapshot_wire import _disabled_trigger_snapshot, snapshot_envelope
from memory_apply_item import read_item
from memory_kernel_apply import MemoryControlState

router = APIRouter()

HEAD = '''SELECT COALESCE(account.account_generation,0) AS trusted_generation,
control.control_json,control.account_generation,control.head_commit_id,
control.commit_sequence,control.source_generation,
EXISTS(SELECT 1 FROM cf_memories WHERE uid=owner.uid) AS has_memory,
(EXISTS(SELECT 1 FROM cf_account_deletion_intents WHERE uid=owner.uid) OR
EXISTS(SELECT 1 FROM cf_account_deletion_tombstones WHERE uid=owner.uid)) AS deleted
FROM (SELECT ? AS uid) owner
LEFT JOIN cf_account_cutover account ON account.uid=owner.uid
LEFT JOIN cf_memory_apply_control control ON control.uid=owner.uid'''
TRIGGERS = '''SELECT * FROM cf_memories WHERE uid=?
AND json_extract(canonical_metadata_json,'$.kind')='trigger' ORDER BY id LIMIT ?'''


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class TriggerSnapshotStore:
    def __init__(self, env, uid):
        self.db, self.uid = env.APP_DB, uid
        self.fence, self.rows_digest = None, None

    def failure(self, reason):
        return V3TrustedAccountGenerationResult(
            uid=self.uid, source_path='cf_account_cutover/cf_memory_apply_control', read_error_reason=reason
        )

    async def raw_triggers(self, limit):
        result = await self.db.prepare(TRIGGERS).bind(self.uid, limit).all()
        if not isinstance(result, dict) or not isinstance(result.get('results'), list):
            raise ValueError('trigger query result is unavailable')
        return result['results']

    async def head(self):
        try:
            row = await self.db.prepare(HEAD).bind(self.uid).first()
            if not row or row['deleted']:
                return self.failure(Failure.READ_FAILED)
            current = digest(row)
            if self.fence is not None and current != self.fence:
                return self.failure(Failure.READ_FAILED)
            self.fence = current
            generation = row['trusted_generation']
            if type(generation) is not int or generation < 0:
                return self.failure(Failure.MALFORMED_ACCOUNT_GENERATION)
            if row['control_json'] is None:
                # A historical row without canonical control is not evidence
                # that an unseen watchlist is empty.
                return self.failure(Failure.MALFORMED_STATE_HEAD if row['has_memory'] else Failure.MISSING_STATE_HEAD)
            control = MemoryControlState.model_validate_json(row['control_json'])
            if (
                control.uid != self.uid
                or control.account_generation != generation
                or not control.head_commit_id
                or type(row['commit_sequence']) is not int
                or row['commit_sequence'] < 0
                or any(
                    row[field] != getattr(control, field)
                    for field in ('account_generation', 'head_commit_id', 'commit_sequence', 'source_generation')
                )
            ):
                return self.failure(Failure.MALFORMED_STATE_HEAD)
            if self.rows_digest is not None:
                # Historical writers have not all converged on the ledger
                # head. Fence the actual bounded row set as well as that head.
                if digest(await self.raw_triggers(MAX_AUTHORITATIVE_TRIGGERS + 1)) != self.rows_digest:
                    return self.failure(Failure.READ_FAILED)
            return V3TrustedAccountGenerationResult(
                uid=self.uid,
                source_path='cf_account_cutover/cf_memory_apply_control',
                account_generation=generation,
                head_commit_id=control.head_commit_id,
                commit_sequence=control.commit_sequence,
            )
        except Exception:
            return self.failure(Failure.READ_FAILED)

    async def triggers(self, limit):
        rows = await self.raw_triggers(limit)
        self.rows_digest = digest(rows)
        snapshots = []
        for row in rows:
            try:
                item = read_item(row).model_dump(mode='python')
            except (ValueError, TypeError, KeyError):
                item = None
            snapshots.append({'id': row['id'], 'item': item})
        return snapshots


@router.get('/v1/jit/trigger-snapshot')
async def trigger_snapshot(request: Request):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    uid = str(principal['uid'])
    env = request.scope['env']
    decision, _, _ = await resolve(env, uid)
    result = _disabled_trigger_snapshot(uid)
    if decision.permits_work:
        snapshot = await read_authoritative_trigger_snapshot(uid, store=TriggerSnapshotStore(env, uid))
        final_decision, _, _ = await resolve(env, uid)
        if final_decision.permits_work:
            result = snapshot_envelope(snapshot)
    return JSONResponse(result.model_dump(mode='json'), headers={'Cache-Control': 'no-store'})
