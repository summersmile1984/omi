"""Fail-closed task workflow control for Cloudflare-native accounts."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_auth import decode_context

router = APIRouter()


def _context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


@router.get("/v1/candidates/control")
async def get_candidate_workflow_control(request: Request):
    """Return the safe default until task-control state has a D1 projection.

    The legacy control document is Firestore-backed. Cloudflare-native staging
    accounts have no imported control state yet, so the only safe response is
    the model's default ``off`` shell with generation zero and no Chat-first UI.
    This keeps released clients on the existing shell instead of exposing a
    partial task-intelligence capability.
    """

    if not _context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {
        "workflow_mode": "off",
        "account_generation": 0,
        "chat_first_ui": False,
    }
