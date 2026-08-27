from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from workers import asgi

from internal_auth import decode_context

app = FastAPI(title="Omi Cloudflare AI API", version="0.1.0")


def auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "api-ai", "version": "cf-02"}


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> Any:
    if not auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    base_url = getattr(env, "EMBEDDING_API_BASE_URL", None)
    api_key = getattr(env, "EMBEDDING_API_KEY", None)
    if not base_url or not api_key:
        return JSONResponse({"error": "embedding provider is not configured"}, status_code=503)
    body = await request.json()
    model = body.get("model") or getattr(env, "EMBEDDING_MODEL", "text-embedding-3-small")
    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/v1/embeddings",
            headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json={"model": model, "input": body.get("input")},
        )
    return JSONResponse(response.json(), status_code=response.status_code)


Default = asgi.entrypoint(app)
