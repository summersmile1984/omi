import base64
import inspect
import json
import math
import time
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
from auto_model_routes import router as auto_model_router
from realtime_routes import router as realtime_router

app = FastAPI(title="Omi Cloudflare AI API", version="0.1.0")
app.include_router(auto_model_router)
app.include_router(realtime_router)

MAX_TRANSCRIPTION_BODY_BYTES = 25_000_000
MAX_WORKERS_AI_AUDIO_BYTES = 5_000_000
MAX_EMBEDDING_ITEMS = 32
MAX_EMBEDDING_TEXT_CHARS = 4_096
MAX_EMBEDDING_TOTAL_CHARS = 32_000
MAX_TRANSLATION_ITEMS = 32
MAX_TRANSLATION_TEXT_CHARS = 4_096
MAX_TRANSLATION_TOTAL_CHARS = 32_000
MAX_AI_BODY_BYTES = 8_000_000
MAX_AI_RESPONSE_BYTES = 12_000_000
MAX_TTS_CHARS = 4_096
MAX_WORKERS_AI_TTS_CHARS = 4_096
DEFAULT_WORKERS_AI_EMBEDDING_MODEL = "@cf/baai/bge-base-en-v1.5"
WORKERS_AI_TTS_SPEAKERS = frozenset(
    {"angus", "asteria", "arcas", "orion", "orpheus", "athena", "luna", "zeus", "perseus", "helios", "hera", "stella"}
)
OPENAI_TTS_VOICES = frozenset(
    {"alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse", "marin", "cedar"}
)


class TtsSynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TTS_CHARS)
    voice_id: str
    instructions: str | None = None


class WorkersAiTtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_WORKERS_AI_TTS_CHARS)
    speaker: str = Field(default="angus", min_length=2, max_length=32)


class TranslationRequest(BaseModel):
    contents: list[str] = Field(..., max_length=MAX_TRANSLATION_ITEMS)
    target_language_code: str
    source_language_code: str | None = None
    request_id: str | None = None


class WorkersAiEmbeddingsRequest(BaseModel):
    input: str | list[str]
    model: str | None = None


# m2m100's published Workers AI surface currently documents these languages.
# Keep the native route's capability claim explicit; the legacy NLLB service
# remains the fallback for its broader language matrix.
WORKERS_AI_TRANSLATION_LANGUAGES = {
    "en": "english",
    "zh": "chinese",
    "fr": "french",
    "es": "spanish",
    "ar": "arabic",
    "ru": "russian",
    "de": "german",
    "ja": "japanese",
    "pt": "portuguese",
    "hi": "hindi",
}
WORKERS_AI_TRANSLATION_LANGUAGE_ALIASES = {
    language: code for code, language in WORKERS_AI_TRANSLATION_LANGUAGES.items()
}


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


def _embedding_input(payload: WorkersAiEmbeddingsRequest) -> list[str] | None:
    values = [payload.input] if isinstance(payload.input, str) else payload.input
    if not values or len(values) > MAX_EMBEDDING_ITEMS:
        return None
    if any(not isinstance(value, str) or len(value) > MAX_EMBEDDING_TEXT_CHARS for value in values):
        return None
    if sum(len(value) for value in values) > MAX_EMBEDDING_TOTAL_CHARS:
        return None
    return values


def _embedding_vectors(result: object) -> list[list[float]] | None:
    payload = _workers_ai_result_mapping(result)
    raw_vectors = payload.get("data")
    if not isinstance(raw_vectors, list):
        return None
    vectors: list[list[float]] = []
    for raw_vector in raw_vectors:
        if not isinstance(raw_vector, list) or not raw_vector:
            return None
        vector: list[float] = []
        for value in raw_vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                return None
            vector.append(float(value))
        vectors.append(vector)
    return vectors


@app.post("/v1/embeddings-workers-ai")
async def embeddings_workers_ai(request: Request):
    """Generate bounded text embeddings with the native Workers AI BGE binding.

    This route is additive: the existing `/v1/embeddings` provider proxy keeps
    its configured model and response untouched, while callers can explicitly
    opt into the fixed 768-dimensional BGE model exposed by Workers AI.
    """
    if not auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = WorkersAiEmbeddingsRequest.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid embeddings request"}, status_code=400)
    values = _embedding_input(payload)
    if values is None:
        return JSONResponse({"error": "embedding input is too large or empty"}, status_code=413)

    env = request.scope["env"]
    ai = getattr(env, "AI", None)
    if ai is None:
        return JSONResponse({"error": "workers ai is not configured"}, status_code=503)
    model = getattr(env, "WORKERS_AI_EMBEDDING_MODEL", DEFAULT_WORKERS_AI_EMBEDDING_MODEL)
    try:
        result = await ai.run(model, {"text": values})
        vectors = _embedding_vectors(result)
    except Exception:
        return JSONResponse({"error": "workers ai embeddings unavailable"}, status_code=502)
    if vectors is None or len(vectors) != len(values):
        return JSONResponse({"error": "workers ai returned invalid embeddings"}, status_code=502)
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": vector, "index": index}
            for index, vector in enumerate(vectors)
        ],
        "model": model,
    }


def _ai_upstream_url(base_url: str, request: Request) -> str:
    """Map `/v1/ai/*` to a fixed provider base without accepting a URL from the client."""
    path = request.url.path
    suffix = path[len("/v1/ai") :] if path.startswith("/v1/ai") else "/"
    if not suffix:
        suffix = "/"
    query = request.url.query
    url = f"{base_url.rstrip('/')}/{suffix.lstrip('/')}"
    return f"{url}?{query}" if query else url


def _ai_request_headers(request: Request, api_key: str) -> dict[str, str]:
    """Forward only protocol headers; never forward client credentials to the provider."""
    headers = {"authorization": f"Bearer {api_key}"}
    for name in ("content-type", "accept"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


@app.api_route("/v1/ai/{path:path}", methods=["GET", "POST"])
async def ai_proxy(request: Request, path: str):
    """Proxy an authenticated API-compatible AI request to one configured provider.

    The client cannot select a host: `AI_API_BASE_URL` is a Worker secret and the
    route only appends the path after `/v1/ai`. This keeps provider SDKs and local
    model runtimes out of the Python Worker while allowing OpenAI-compatible LLM
    and tool endpoints to share the existing Edge/API-AI deployment surface.
    """
    if not auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    base_url = getattr(env, "AI_API_BASE_URL", None)
    api_key = getattr(env, "AI_API_KEY", None)
    if not base_url or not api_key:
        return JSONResponse({"error": "ai provider is not configured"}, status_code=503)

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_AI_BODY_BYTES:
        return JSONResponse({"error": "ai request body too large"}, status_code=413)
    body = b""
    if request.method != "GET":
        body = await request.body()
        if len(body) > MAX_AI_BODY_BYTES:
            return JSONResponse({"error": "ai request body too large"}, status_code=413)

    if worker_fetch is None:
        return JSONResponse({"error": "worker fetch is unavailable"}, status_code=503)
    try:
        fetch_options = {
            "method": request.method,
            "headers": _ai_request_headers(request, api_key),
        }
        if request.method != "GET":
            fetch_options["body"] = body
        response = await worker_fetch(_ai_upstream_url(base_url, request), **fetch_options)
        response_body = bytes(await response.arrayBuffer())
    except (OSError, TypeError, ValueError):
        return JSONResponse({"error": "ai upstream unavailable"}, status_code=502)
    if len(response_body) > MAX_AI_RESPONSE_BYTES:
        return JSONResponse({"error": "ai upstream response too large"}, status_code=502)

    content_type = response.headers.get("content-type", "application/json")
    return Response(content=response_body, status_code=int(response.status), media_type=content_type)


def _provider_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _workers_ai_result_mapping(result: object) -> dict[str, object]:
    """Convert a Workers AI FFI result without leaking a JS proxy to FastAPI."""
    if isinstance(result, dict):
        return result
    to_py = getattr(result, "to_py", None)
    if callable(to_py):
        converted = to_py()
        if isinstance(converted, dict):
            return converted
    fields: dict[str, object] = {}
    for field in (
        "text",
        "translated_text",
        "word_count",
        "words",
        "segments",
        "vtt",
        "detected_language",
        "transcription_info",
    ):
        value = getattr(result, field, None)
        value_to_py = getattr(value, "to_py", None)
        if callable(value_to_py):
            value = value_to_py()
        if value is not None:
            fields[field] = value
    return fields


def _coerce_audio_bytes(value: object) -> bytes | None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    to_py = getattr(value, "to_py", None)
    if callable(to_py):
        converted = to_py()
        if converted is not value:
            return _coerce_audio_bytes(converted)
    return None


async def _workers_ai_audio_bytes(result: object) -> bytes | None:
    """Extract binary model output across Python Workers FFI result shapes."""
    audio = _coerce_audio_bytes(result)
    if audio is not None:
        return audio
    for method_name in ("bytes", "buffer", "arrayBuffer"):
        method = getattr(result, method_name, None)
        if not callable(method):
            continue
        value = method()
        if inspect.isawaitable(value):
            value = await value
        audio = _coerce_audio_bytes(value)
        if audio is not None:
            return audio
    return None


def _normalize_workers_ai_language(raw: object) -> tuple[str, str] | None:
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower().replace("_", "-")
    if not normalized:
        return None
    base = normalized.split("-", 1)[0]
    code = base if base in WORKERS_AI_TRANSLATION_LANGUAGES else WORKERS_AI_TRANSLATION_LANGUAGE_ALIASES.get(base)
    if code is None:
        return None
    return code, WORKERS_AI_TRANSLATION_LANGUAGES[code]


@app.post("/v1/translate")
async def translate_workers_ai(request: Request):
    """Translate a bounded batch using the native Workers AI m2m100 binding.

    The request and response deliberately match the standalone NLLB service so
    callers can switch the Edge route without learning a second contract. This
    route only advertises the language subset documented by the bound model;
    broader languages and quality qualification remain on the legacy service.
    """
    if not auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = TranslationRequest.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid translation request"}, status_code=400)
    if any(len(content) > MAX_TRANSLATION_TEXT_CHARS for content in payload.contents):
        return JSONResponse({"error": "translation content is too long"}, status_code=413)
    if sum(len(content) for content in payload.contents) > MAX_TRANSLATION_TOTAL_CHARS:
        return JSONResponse({"error": "translation request is too large"}, status_code=413)

    target = _normalize_workers_ai_language(payload.target_language_code)
    if target is None:
        return JSONResponse({"error": "unsupported target language"}, status_code=400)
    source = _normalize_workers_ai_language(payload.source_language_code) if payload.source_language_code else None
    if payload.source_language_code and source is None:
        return JSONResponse({"error": "unsupported source language"}, status_code=400)
    if not payload.contents:
        return {
            "translations": [],
            "model": getattr(request.scope["env"], "WORKERS_AI_TRANSLATION_MODEL", "@cf/meta/m2m100-1.2b"),
            "latency_ms": 0,
        }

    env = request.scope["env"]
    ai = getattr(env, "AI", None)
    if ai is None:
        return JSONResponse({"error": "workers ai is not configured"}, status_code=503)
    model = getattr(env, "WORKERS_AI_TRANSLATION_MODEL", "@cf/meta/m2m100-1.2b")
    source_code, source_name = source or ("", "english")
    _, target_name = target
    started = time.perf_counter()
    translations: list[dict[str, str]] = []
    try:
        for content in payload.contents:
            result = await ai.run(
                model,
                {"text": content, "source_lang": source_name, "target_lang": target_name},
            )
            result_payload = _workers_ai_result_mapping(result)
            translated = result_payload.get("translated_text")
            if not isinstance(translated, str):
                return JSONResponse({"error": "workers ai returned an invalid translation"}, status_code=502)
            translations.append(
                {
                    "translated_text": translated,
                    "detected_language_code": source_code,
                }
            )
    except Exception:
        return JSONResponse({"error": "workers ai translation unavailable"}, status_code=502)

    return {
        "translations": translations,
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }


@app.post("/v1/stt/transcribe-workers-ai")
async def transcribe_workers_ai(request: Request):
    """Transcribe a raw audio body with a native Workers AI binding.

    This is deliberately additive: the existing multipart `/v1/stt/transcribe`
    contract remains on the configured hosted provider because it supports
    diarization and the legacy segment shape. This route is a small, bounded
    Workers AI seam for clients that can send the provider's binary input.
    """
    if not auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    content_type = request.headers.get("content-type", "application/octet-stream").lower()
    if not (content_type.startswith("audio/") or content_type == "application/octet-stream"):
        return JSONResponse({"error": "workers ai transcription expects a raw audio body"}, status_code=415)
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_WORKERS_AI_AUDIO_BYTES:
        return JSONResponse({"error": "workers ai audio body too large"}, status_code=413)
    body = await request.body()
    if not body:
        return JSONResponse({"error": "no audio data provided"}, status_code=400)
    if len(body) > MAX_WORKERS_AI_AUDIO_BYTES:
        return JSONResponse({"error": "workers ai audio body too large"}, status_code=413)

    env = request.scope["env"]
    ai = getattr(env, "AI", None)
    if ai is None:
        return JSONResponse({"error": "workers ai is not configured"}, status_code=503)
    model = getattr(env, "WORKERS_AI_ASR_MODEL", "@cf/openai/whisper-large-v3-turbo")
    try:
        # Workers AI's Whisper binding accepts the binary input as a base64
        # string. Keeping the conversion here avoids exposing a JS typed-array
        # requirement to callers of this Python route.
        result = await ai.run(model, {"audio": base64.b64encode(body).decode("ascii")})
        payload = _workers_ai_result_mapping(result)
    except Exception:
        return JSONResponse({"error": "workers ai transcription unavailable"}, status_code=502)

    text = payload.get("text")
    if not isinstance(text, str):
        return JSONResponse({"error": "workers ai returned an invalid transcription"}, status_code=502)
    info = payload.get("transcription_info")
    detected_language = payload.get("detected_language")
    if detected_language is None and isinstance(info, dict):
        detected_language = info.get("language")
    response: dict[str, object] = {
        "text": text,
        "segments": payload.get("segments", []),
        "detected_language": detected_language,
        "provider": "workers-ai",
        "model": model,
    }
    for field in ("word_count", "words", "vtt"):
        if field in payload:
            response[field] = payload[field]
    return response


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


@app.post("/v1/tts/synthesize-workers-ai")
async def tts_synthesize_workers_ai(request: Request):
    """Synthesize bounded text using the native Workers AI Aura binding.

    This is additive because Aura exposes its own speaker IDs rather than the
    existing provider-specific voice IDs. The legacy `/v1/tts/synthesize`
    contract stays on the external provider until voice parity and quality are
    qualified.
    """
    if not auth_context(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = WorkersAiTtsRequest.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return JSONResponse({"error": "invalid workers ai tts request"}, status_code=400)
    text = payload.text.strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    speaker = payload.speaker.strip().lower()
    if speaker not in WORKERS_AI_TTS_SPEAKERS:
        return JSONResponse({"error": "unsupported speaker"}, status_code=400)

    env = request.scope["env"]
    ai = getattr(env, "AI", None)
    if ai is None:
        return JSONResponse({"error": "workers ai is not configured"}, status_code=503)
    model = getattr(env, "WORKERS_AI_TTS_MODEL", "@cf/deepgram/aura-1")
    try:
        # `returnRawResponse` keeps the model's MPEG stream intact instead of
        # forcing a JSON/base64 envelope through the Python FFI boundary.
        result = await ai.run(
            model,
            {"text": text, "speaker": speaker, "encoding": "mp3"},
            {"returnRawResponse": True},
        )
        audio = await _workers_ai_audio_bytes(result)
    except Exception:
        # Workers AI surfaces provider/model failures as runtime-specific FFI
        # exceptions (for example `JsException`), so keep those out of the
        # ASGI error page and expose the route's stable upstream error.
        return JSONResponse({"error": "workers ai tts failed"}, status_code=502)
    if not audio:
        return JSONResponse({"error": "workers ai tts returned empty audio"}, status_code=502)
    return Response(content=audio, media_type="audio/mpeg")


Default = asgi.entrypoint(app) if asgi else None
