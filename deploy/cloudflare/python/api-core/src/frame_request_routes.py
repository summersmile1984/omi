"""Internal Core frame metadata endpoints; Edge waits for the complete family."""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request, File, UploadFile

import frame_request_store as store
import frame_request_pixels as pixels
from frame_request_image import _ALLOWED_IMAGE_FORMATS
from frame_image_transform import canonicalize_frame_image
from frame_upload_form import FrameUploadRoute, MAX_FILE_BYTES
from frame_request_contract import (
    CreateFrameRequest,
    FrameRequestBatch,
    FrameRequestEnvelope,
    FrameRequestStateUpdate,
    FrameRequestPromotion,
)
from jit_authority import authorize, resolve
from screen_frame_views import ScreenshotRoute, _response, owner

router = APIRouter(route_class=ScreenshotRoute)
upload_router = APIRouter(route_class=FrameUploadRoute)


@router.get('/v1/jit/rollout-decision')
async def decision(request: Request, uid: str = Depends(owner)):
    result, _, _ = await resolve(request.scope['env'], uid)
    return _response(asdict(result))


@router.post('/v1/frame-requests')
async def create(request: Request, body: CreateFrameRequest, uid: str = Depends(owner)):
    result, deduplicated = await store.create(request.scope['env'], uid, body)
    return _response(FrameRequestEnvelope(request=result, deduplicated=deduplicated).model_dump(mode='json'))


@router.get('/v1/frame-requests/status/{request_id}')
async def status(
    request: Request, request_id: str, account_generation: int = Query(default=0, ge=0), uid: str = Depends(owner)
):
    env = request.scope['env']
    snapshot = await authorize(env, uid, account_generation)
    result = await store.get(env, uid, request_id, snapshot)
    if result.account_generation != account_generation:
        raise HTTPException(404, 'frame_request_not_found')
    return _response(FrameRequestEnvelope(request=result).model_dump(mode='json'))


@router.get('/v1/frame-requests/pending')
async def pending(
    request: Request,
    device_id: str = Query(min_length=1, max_length=256),
    account_generation: int = Query(default=0, ge=0),
    limit: int = Query(default=32, ge=1, le=32),
    uid: str = Depends(owner),
):
    rows = await store.pending(request.scope['env'], uid, device_id, account_generation, limit)
    return _response(FrameRequestBatch(requests=rows).model_dump(mode='json'))


@router.post('/v1/frame-requests/{request_id}/state')
async def transition(request: Request, request_id: str, body: FrameRequestStateUpdate, uid: str = Depends(owner)):
    result = await store.transition(request.scope['env'], uid, request_id, body)
    return _response(FrameRequestEnvelope(request=result).model_dump(mode='json'))


@upload_router.post('/v1/frame-requests/{request_id}/upload')
async def upload(
    request: Request,
    request_id: str,
    device_id: str,
    account_generation: int,
    file: UploadFile = File(...),
    uid: str = Depends(owner),
):
    env = request.scope['env']
    await authorize(env, uid, account_generation)
    if not file.content_type or file.content_type.lower() not in set(_ALLOWED_IMAGE_FORMATS.values()):
        raise HTTPException(415, 'frame_upload_requires_image')
    payload = await file.read(MAX_FILE_BYTES + 1)
    if len(payload) > MAX_FILE_BYTES:
        raise HTTPException(413, 'frame_upload_too_large')
    canonical = await canonicalize_frame_image(env, payload)
    result = await pixels.upload(env, uid, request_id, device_id, account_generation, canonical)
    return _response(FrameRequestEnvelope(request=result).model_dump(mode='json'))


router.include_router(upload_router)


@router.post('/v1/frame-requests/{request_id}/promote')
async def promote(request: Request, request_id: str, promotion: FrameRequestPromotion, uid: str = Depends(owner)):
    result = await pixels.promote(request.scope['env'], uid, request_id, promotion)
    return _response(FrameRequestEnvelope(request=result).model_dump(mode='json'))


@router.get('/v1/frame-requests/temporary/{request_id}/image')
async def temporary_image(
    request: Request, request_id: str, account_generation: int = Query(default=0, ge=0), uid: str = Depends(owner)
):
    return await pixels.temporary_image(request.scope['env'], uid, request_id, account_generation)


@router.get('/v1/conversations/{conversation_id}/photos/{photo_id}/image')
async def conversation_image(request: Request, conversation_id: str, photo_id: str, uid: str = Depends(owner)):
    return await pixels.conversation_image(request.scope['env'], uid, conversation_id, photo_id)
