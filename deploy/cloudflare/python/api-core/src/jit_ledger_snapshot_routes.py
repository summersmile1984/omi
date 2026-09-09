"""Original desktop ledger prompt/mirror wire contracts over authenticated D1."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from candidate_routes import context
from jit_authority import resolve
from jit_ledger_snapshot_kernel import (
    DEFAULT_MIRROR_PAGE_SIZE,
    LedgerPromptSnapshotMode,
    _build_enabled_snapshot,
    _disabled_snapshot,
    _disabled_mirror_snapshot,
    mirror_envelope,
    read_authoritative_ledger_mirror_page,
)
from jit_ledger_snapshot_store import LedgerSnapshotStore

router = APIRouter()
HEADERS = {'Cache-Control': 'no-store'}


def reply(value):
    return JSONResponse(value.model_dump(mode='json'), headers=HEADERS)


@router.get('/v1/jit/knowledge-ledger/prompt-snapshot')
async def prompt_snapshot(request: Request):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401, headers=HEADERS)
    uid, env = str(principal['uid']), request.scope['env']
    decision, generation, _ = await resolve(env, uid)
    if not decision.permits_work:
        return reply(_disabled_snapshot(decision))
    store = LedgerSnapshotStore(env, uid)
    try:
        snapshot = await _build_enabled_snapshot(uid, store=store)
        if snapshot.mode != LedgerPromptSnapshotMode.enabled:
            return reply(snapshot)
        await store.verify_prompt()
        final_decision, final_generation, _ = await resolve(env, uid)
        if not final_decision.permits_work:
            return reply(_disabled_snapshot(final_decision))
        if generation != final_generation:
            raise ValueError('ledger snapshot generation changed')
        return reply(snapshot)
    except Exception as exc:
        raise HTTPException(503, 'Ledger prompt snapshot unavailable', headers=HEADERS) from exc


@router.get('/v1/jit/knowledge-ledger/mirror-snapshot')
async def mirror_snapshot(request: Request, cursor: str | None = None, page_size: int = DEFAULT_MIRROR_PAGE_SIZE):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401, headers=HEADERS)
    uid, env = str(principal['uid']), request.scope['env']
    decision, generation, _ = await resolve(env, uid)
    if not decision.permits_work:
        return reply(_disabled_mirror_snapshot(uid, 'rollout_not_enabled'))
    store = LedgerSnapshotStore(env, uid)
    page = await read_authoritative_ledger_mirror_page(uid, cursor=cursor, page_size=page_size, store=store)
    final_decision, final_generation, _ = await resolve(env, uid)
    if not final_decision.permits_work:
        return reply(_disabled_mirror_snapshot(uid, 'rollout_not_enabled'))
    if generation != final_generation:
        return reply(_disabled_mirror_snapshot(uid, 'authority_changed'))
    return reply(mirror_envelope(uid, page))
