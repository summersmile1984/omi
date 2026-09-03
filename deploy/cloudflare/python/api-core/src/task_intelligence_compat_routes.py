"""Fail-closed read boundaries for the not-yet-projected task intelligence APIs.

The legacy What Matters Now read is not a pure D1 projection: it evaluates
Firestore-backed candidates, device snapshots, open loops, and an LLM
judgment.  Cloudflare has no canonical candidate/recommendation projection,
so this module authenticates the request and returns the released disabled
shell (404) rather than inventing an empty recommendation result.
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


def _feature_unavailable(request: Request) -> JSONResponse:
    if not _auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"detail": "Not found"}, status_code=404)


@router.get("/v1/what-matters-now")
async def get_what_matters_now(request: Request):
    """Keep the read path closed until candidate and recommendation authority exists."""

    return _feature_unavailable(request)


@router.get("/v1/task-intelligence/debug/evaluations/{evaluation_id}")
async def get_evaluation_debug_projection(request: Request, evaluation_id: str):
    """Do not expose Firestore/LLM debug projections from API Core."""

    return _feature_unavailable(request)


__all__ = ["get_evaluation_debug_projection", "get_what_matters_now", "router"]
