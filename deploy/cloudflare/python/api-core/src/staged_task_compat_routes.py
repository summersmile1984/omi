"""Fail-closed compatibility routes for the retired staged-task surface.

The released staged-task API is backed by the legacy Firestore candidate
projection. Cloudflare staging does not yet import that projection or its
generation fence, so authenticated callers receive the same feature-disabled
404 boundary as the candidate endpoints instead of falling through to the
legacy runtime.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _feature_disabled(request: Request) -> JSONResponse:
    if not _auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"detail": "Not found"}, status_code=404)


@router.delete("/v1/staged-tasks")
async def clear_staged_tasks(request: Request):
    return _feature_disabled(request)


@router.delete("/v1/staged-tasks/{task_id}")
async def delete_staged_task(request: Request, task_id: str):
    return _feature_disabled(request)


@router.get("/v1/staged-tasks")
async def list_staged_tasks(request: Request):
    return _feature_disabled(request)


@router.patch("/v1/staged-tasks/batch-scores")
async def update_staged_scores(request: Request):
    return _feature_disabled(request)


@router.post("/v1/staged-tasks")
async def create_staged_task(request: Request):
    return _feature_disabled(request)


@router.post("/v1/staged-tasks/promote")
async def promote_staged_task(request: Request):
    return _feature_disabled(request)


@router.post("/v1/staged-tasks/{task_id}/promote")
async def promote_staged_task_by_id(request: Request, task_id: str):
    return _feature_disabled(request)


__all__ = ["router"]
