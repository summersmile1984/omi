from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

try:
    from workers import asgi
except ModuleNotFoundError as error:  # CPython unit tests do not provide Pyodide's `js` module.
    if error.name != "js":
        raise
    asgi = None  # type: ignore[assignment]

from internal_auth import decode_context

app = FastAPI(title="Omi Cloudflare AI API", version="0.1.0")

MAX_TRANSCRIPTION_BODY_BYTES = 25_000_000


def auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "api-ai", "version": "cf-03"}


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


def _provider_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


@app.post("/v1/stt/transcribe")
async def transcribe(request: Request):
    """Proxy the existing multipart STT contract to a hosted provider.

    The Worker never loads a speech model. A provider-compatible `/v2/transcribe`
    endpoint receives the original multipart body, so clients keep the existing
    `file`/`diarize` contract while the execution moves out of the Worker.
    """
    if not auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    base_url = getattr(env, "ASR_API_BASE_URL", None)
    api_key = getattr(env, "ASR_API_KEY", None)
    if not base_url or not api_key:
        return JSONResponse({"error": "transcription provider is not configured"}, status_code=503)

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_TRANSCRIPTION_BODY_BYTES:
        return JSONResponse({"error": "transcription body too large"}, status_code=413)
    body = await request.body()
    if not body:
        return JSONResponse({"error": "no audio data provided"}, status_code=400)
    if len(body) > MAX_TRANSCRIPTION_BODY_BYTES:
        return JSONResponse({"error": "transcription body too large"}, status_code=413)

    headers = {"content-type": request.headers.get("content-type", "application/octet-stream")}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(_provider_url(base_url, "/v2/transcribe"), headers=headers, content=body)
    except httpx.HTTPError:
        return JSONResponse({"error": "transcription upstream unavailable"}, status_code=502)

    content_type = response.headers.get("content-type", "application/json")
    if content_type.startswith("application/json"):
        try:
            return JSONResponse(response.json(), status_code=response.status_code)
        except ValueError:
            return JSONResponse({"error": "transcription upstream returned invalid JSON"}, status_code=502)
    return JSONResponse({"error": "transcription upstream returned unsupported content"}, status_code=502)


Default = asgi.entrypoint(app) if asgi else None
