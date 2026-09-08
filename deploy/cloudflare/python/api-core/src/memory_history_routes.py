"""Canonical history keeps the upstream policy and bare-array wire contract."""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from candidate_routes import context
from jit_authority import resolve
from memory_history_kernel import ListReadBudget, read_ledger_history_page
from memory_history_store import HistoryStore
from memory_history_wire import MemoryApiExposure, memory_api_payloads

router = APIRouter()


@router.get('/v3/memories/ledger-history')
async def history(request: Request, limit: int = 100, offset: int = 0):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    uid, env = str(principal['uid']), request.scope['env']
    headers = {'Cache-Control': 'no-store'}
    decision, generation, _ = await resolve(env, uid)
    if not decision.permits_work:
        return JSONResponse([], headers=headers)
    budget = ListReadBudget.for_request(request, route='memories-ledger-history')
    store = HistoryStore(env, uid)
    try:
        await store.head()
        if store.generation != generation:
            raise ValueError('memory history generation changed')
        page = await read_ledger_history_page(store, uid, limit=limit, offset=offset, budget=budget)
        # Reserve the original serialization headroom for final authority checks.
        # If those cannot finish, no previously-read content is returned.
        async with asyncio.timeout(6.0):
            await store.verify()
        final_decision, final_generation, _ = await resolve(env, uid)
        if not final_decision.permits_work:
            return JSONResponse([], headers=headers)
        if final_generation != generation:
            raise ValueError('memory history generation changed')
        if budget.truncated or page.truncated:
            headers['X-Omi-List-Truncated'] = 'true'
        payload = memory_api_payloads(page.memories, MemoryApiExposure.CANONICAL)
        return JSONResponse(jsonable_encoder(payload), headers=headers)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, 'Ledger history unavailable', headers=headers) from exc
