"""Fail-closed compatibility routes for the retired task-candidate surface.

The isolated Cloudflare profile does not project the legacy Firestore candidate
store or its generation-scoped task workflow controls.  The canonical control
route therefore returns the closed shell (workflow off).  These compatibility
handlers preserve the legacy router's feature-disabled response (404) after
the Edge has authenticated the Better Auth principal, instead of forwarding a
Cloudflare account to the retired Firestore owner.
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


@router.get("/v1/candidates")
async def list_candidates(request: Request):
    return _feature_disabled(request)


@router.get("/v1/candidates/{candidate_id}")
async def get_candidate(request: Request, candidate_id: str):
    return _feature_disabled(request)


@router.post("/v1/candidates")
async def create_candidate(request: Request):
    return _feature_disabled(request)


@router.post("/v1/candidates/migrate-staged")
async def migrate_staged_candidates(request: Request):
    return _feature_disabled(request)


@router.post("/v1/candidates/integrations/drain")
async def drain_candidate_integrations(request: Request):
    return _feature_disabled(request)


@router.post("/v1/candidates/{candidate_id}/accept")
async def accept_candidate(request: Request, candidate_id: str):
    return _feature_disabled(request)


@router.post("/v1/candidates/{candidate_id}/reject")
async def reject_candidate(request: Request, candidate_id: str):
    return _feature_disabled(request)


@router.post("/v1/candidates/{candidate_id}/expire")
async def expire_candidate(request: Request, candidate_id: str):
    return _feature_disabled(request)


__all__ = ["router"]
