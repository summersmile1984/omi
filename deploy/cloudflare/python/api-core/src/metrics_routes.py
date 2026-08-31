"""Fail-closed operational metrics boundary for the Cloudflare API Core.

The legacy ``/metrics`` route exposes a process-local Prometheus registry.
Cloudflare API Core has no equivalent durable scrape authority yet, so this
route deliberately never emits an empty or synthetic exposition.  It keeps
the bearer-secret boundary in place and returns an explicit unavailable
response until a reviewed Cloudflare metrics authority is provisioned.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


router = APIRouter()


def _unavailable() -> JSONResponse:
    return JSONResponse(
        {"error": "metrics_unavailable"},
        status_code=503,
        headers={"cache-control": "no-store"},
    )


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"detail": "Unauthorized"},
        status_code=401,
        headers={"cache-control": "no-store"},
    )


@router.get("/metrics")
async def get_metrics(request: Request):
    """Preserve the private scrape boundary without fabricating metrics."""
    expected = getattr(request.scope["env"], "METRICS_SECRET", None)
    if not isinstance(expected, str) or not expected:
        return _unavailable()

    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return _unauthorized()
    token = authorization.removeprefix("Bearer ")
    if not token or not hmac.compare_digest(token, expected):
        return _unauthorized()

    # Prometheus' process-local registry is not available in a stateless
    # Python Worker.  Keep this explicit even when the secret is configured so
    # callers cannot mistake a zero-valued response for migrated authority.
    return _unavailable()
