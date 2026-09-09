"""Authenticated explicit restore; shared upstream policy, no model inference."""

from uuid import UUID
from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from candidate_routes import context
from memory_revert_store import RevertConflict, RevertStore

router = APIRouter()


class MemoryRevertRequest(BaseModel):
    operation_id: UUID


@router.post('/v3/memories/{memory_id}/revert')
async def revert(memory_id: str, body: MemoryRevertRequest, request: Request):
    principal = context(request)
    headers = {'Cache-Control': 'no-store'}
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401, headers=headers)
    try:
        for attempt in range(3):
            store = RevertStore(request.scope['env'], str(principal['uid']))
            try:
                restored = await store.revert_superseded_ledger_fact(store.uid, memory_id, str(body.operation_id))
                await store.verify()
                return JSONResponse(jsonable_encoder({'status': 'ok', 'memory': restored}), headers=headers)
            except HTTPException as exc:
                conflict = exc.__cause__
                if attempt < 2 and isinstance(conflict, RevertConflict) and conflict.retryable:
                    continue
                raise
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **headers}
        raise
    except Exception as exc:
        raise HTTPException(503, 'Knowledge ledger restore unavailable', headers=headers) from exc
