"""The Cloudflare JIT control provider reads App D1 on every boundary."""

import json

from fastapi import HTTPException

from fallback import record_fallback

from jit_policy import (
    JITDecisionReason,
    JITErrorClass,
    JITFlagEvaluation,
    TriState,
    _effective_decision,
    is_jit_admission_allowlisted,
)

# Bind the raw provider snapshot and account generation to the SQL mutation.
# A flag change during a read/prepare gap cannot grant work on a stale decision.
SNAPSHOT = (
    'SELECT json_array('
    '(SELECT rollout FROM cf_jit_flags WHERE uid = ?),'
    '(SELECT kill_switch FROM cf_jit_flags WHERE uid = ?),'
    "(SELECT rollout FROM cf_jit_flags WHERE uid = ''),"
    "(SELECT kill_switch FROM cf_jit_flags WHERE uid = ''),"
    'COALESCE((SELECT account_generation FROM cf_account_cutover WHERE uid = ?),0)) AS snapshot '
    'WHERE NOT EXISTS (SELECT 1 FROM cf_account_deletion_intents WHERE uid = ?) '
    'AND NOT EXISTS (SELECT 1 FROM cf_account_deletion_tombstones WHERE uid = ?)'
)


def bindings(uid):
    return (uid, uid, uid, uid, uid)


async def resolve(env, uid):
    try:
        row = await env.APP_DB.prepare(SNAPSHOT).bind(*bindings(uid)).first()
        if not row:
            raise HTTPException(404, 'frame_requests_unavailable')
        snapshot = row['snapshot']
        user_rollout, user_kill, global_rollout, global_kill, generation = json.loads(snapshot)
        rollout = user_rollout if user_rollout is not None else global_rollout
        # A deployment kill cannot be cleared by a per-user override.
        kill = 1 if global_kill == 1 else user_kill if user_kill is not None else global_kill
        evaluation = JITFlagEvaluation(
            TriState.ENABLED if rollout == 1 else TriState.DISABLED,
            TriState.ENABLED if kill == 1 else TriState.DISABLED,
            JITDecisionReason.FLAG_ABSENT if rollout is None else JITDecisionReason.EVALUATED,
            JITErrorClass.ABSENT if rollout is None else JITErrorClass.NONE,
        )
    except HTTPException:
        raise
    except Exception:
        record_fallback(
            component='auth', from_mode='none', to_mode='none', reason='dependency_unavailable', outcome='exhausted'
        )
        snapshot, generation = None, None
        evaluation = JITFlagEvaluation(
            TriState.UNKNOWN, TriState.UNKNOWN, JITDecisionReason.PROVIDER_ERROR, JITErrorClass.PROVIDER
        )
    decision = _effective_decision(
        evaluation, allowlisted=is_jit_admission_allowlisted(uid), cache_hit=False, cache_ttl_seconds=0
    )
    return decision, generation, snapshot


async def authorize(env, uid, account_generation):
    decision, generation, snapshot = await resolve(env, uid)
    if not decision.permits_work or generation != account_generation or snapshot is None:
        raise HTTPException(404, 'frame_requests_unavailable')
    return snapshot
