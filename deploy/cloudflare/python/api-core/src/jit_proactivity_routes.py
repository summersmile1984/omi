"""Original content-free JIT paid-work admission over canonical D1 authority."""

import json
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from candidate_routes import context
from jit_proactivity_kernel import JITMalformedAuthority, JITProactivityReservationError
from jit_proactivity_store import reserve
from jit_proactivity_wire import JITProactivityReservationRequest, JITProactivityReservationEnvelope
from fallback import record_fallback

router = APIRouter()


@router.post('/v1/jit/proactivity/reservations')
async def reserve_jit_proactivity(request: Request):
    principal = context(request)
    if not principal:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)
    try:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 8192:
                raise ValueError('request too large')
            raw.extend(chunk)
        value = JITProactivityReservationRequest.model_validate(json.loads(raw))
    except (ValueError, TypeError, ValidationError):
        return JSONResponse({'detail': 'Invalid JIT proactivity reservation'}, status_code=422)
    try:
        result = await reserve(request.scope['env'], str(principal['uid']), value)
        if result is None:
            return JSONResponse({'detail': 'JIT proactive work is disabled'}, status_code=403)
        receipt, reserved = result
        return JSONResponse(
            JITProactivityReservationEnvelope(reserved=reserved, receipt=receipt).model_dump(mode='json')
        )
    except (JITProactivityReservationError, ValueError, HTTPException):
        return JSONResponse({'detail': 'JIT proactive budget or authority is unavailable'}, status_code=409)
    except JITMalformedAuthority:
        record_fallback(
            component='other', from_mode='none', to_mode='none', reason='malformed_doc', outcome='exhausted'
        )
        return JSONResponse({'detail': 'JIT proactive authority is temporarily unavailable'}, status_code=503)
    except Exception:
        record_fallback(
            component='other', from_mode='none', to_mode='none', reason='dependency_unavailable', outcome='exhausted'
        )
        return JSONResponse({'detail': 'JIT proactive authority is temporarily unavailable'}, status_code=503)
