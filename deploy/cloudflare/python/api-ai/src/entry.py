import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

try:
    from workers import asgi, fetch as worker_fetch
except ModuleNotFoundError as error:  # CPython unit tests do not provide Pyodide's `js` module.
    if error.name != "js":
        raise
    asgi = None  # type: ignore[assignment]
    worker_fetch = None  # type: ignore[assignment]

from internal_auth import decode_context

app = FastAPI(title="Omi Cloudflare AI API", version="0.1.0")

MAX_TRANSCRIPTION_BODY_BYTES = 25_000_000
MAX_TTS_CHARS = 4_096
OPENAI_TTS_VOICES = frozenset(
    {"alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse", "marin", "cedar"}
)


class TtsSynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TTS_CHARS)
    voice_id: str
    instructions: str | None = None


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
    if worker_fetch is None:
        return JSONResponse({"error": "worker fetch is unavailable"}, status_code=503)
    try:
        response = await worker_fetch(
            f"{base_url.rstrip('/')}/v1/embeddings",
            method="POST",
            headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
            body=json.dumps({"model": model, "input": body.get("input")}),
        )
        return JSONResponse(await response.json(), status_code=int(response.status))
    except (OSError, TypeError, ValueError):
        return JSONResponse({"error": "embedding upstream unavailable"}, status_code=502)


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
        if worker_fetch is None:
            return JSONResponse({"error": "worker fetch is unavailable"}, status_code=503)
        response = await worker_fetch(
            _provider_url(base_url, "/v2/transcribe"), method="POST", headers=headers, body=body
        )
    except (OSError, TypeError, ValueError):
        return JSONResponse({"error": "transcription upstream unavailable"}, status_code=502)

    content_type = response.headers.get("content-type", "application/json")
    if content_type.startswith("application/json"):
        try:
            return JSONResponse(await response.json(), status_code=int(response.status))
        except ValueError:
            return JSONResponse({"error": "transcription upstream returned invalid JSON"}, status_code=502)
    return JSONResponse({"error": "transcription upstream returned unsupported content"}, status_code=502)


@app.post("/v1/tts/synthesize")
async def tts_synthesize(request: Request):
    """Proxy the desktop OpenAI-compatible TTS contract to a hosted API."""
    if not auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = TtsSynthesizeRequest.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid tts request"}, status_code=400)
    text = payload.text.strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    if len(text) > MAX_TTS_CHARS:
        return JSONResponse({"error": "text is too long"}, status_code=400)
    voice_id = payload.voice_id.strip()
    if voice_id not in OPENAI_TTS_VOICES:
        return JSONResponse({"error": "voice_id is not supported"}, status_code=400)

    env = request.scope["env"]
    base_url = getattr(env, "TTS_API_BASE_URL", None)
    api_key = getattr(env, "TTS_API_KEY", None)
    if not base_url or not api_key:
        return JSONResponse({"error": "tts provider is not configured"}, status_code=503)
    if worker_fetch is None:
        return JSONResponse({"error": "worker fetch is unavailable"}, status_code=503)
    provider_payload = {
        "model": getattr(env, "TTS_MODEL", "gpt-4o-mini-tts"),
        "input": text,
        "voice": voice_id,
        "response_format": "mp3",
    }
    if payload.instructions and payload.instructions.strip():
        provider_payload["instructions"] = payload.instructions.strip()
    try:
        response = await worker_fetch(
            f"{base_url.rstrip('/')}/v1/audio/speech",
            method="POST",
            headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
            body=json.dumps(provider_payload),
        )
        if int(response.status) >= 400:
            return JSONResponse({"error": "tts upstream request failed"}, status_code=502)
        audio = bytes(await response.arrayBuffer())
    except (OSError, TypeError, ValueError):
        return JSONResponse({"error": "tts upstream unavailable"}, status_code=502)
    if not audio:
        return JSONResponse({"error": "tts upstream returned empty audio"}, status_code=502)
    return Response(content=audio, media_type="audio/mpeg")


Default = asgi.entrypoint(app) if asgi else None
