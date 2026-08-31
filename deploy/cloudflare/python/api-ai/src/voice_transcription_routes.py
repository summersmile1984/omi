"""Workers AI implementation of the app voice-message transcription contract."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesHeaderParser
from email.policy import default as email_policy
import re
import struct

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from fair_use_meter import content_source_id, record_fair_use_usage, speech_ms_from_transcription
from fair_use_enforcement import fair_use_restriction, fair_use_restriction_response
from internal_auth import decode_context

router = APIRouter()

DEFAULT_WORKERS_AI_ASR_MODEL = "@cf/openai/whisper-large-v3-turbo"
MAX_AUDIO_BYTES = 10 * 1024 * 1024 + 44
MAX_MULTIPART_BODY_BYTES = MAX_AUDIO_BYTES + 64 * 1024
MAX_MULTIPART_FILES = 4
MAX_MULTIPART_PARTS = 8
MAX_MULTIPART_HEADERS_BYTES = 8 * 1024
MAX_LANGUAGE_CHARS = 32
_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?$")
_SUPPORTED_AUDIO_MIME_TYPES = frozenset(
    {
        "",
        "application/octet-stream",
        "audio/mp4",
        "audio/wav",
        "audio/wave",
        "audio/webm",
        "audio/x-wav",
        "video/mp4",
        "video/webm",
    }
)


@dataclass(frozen=True)
class AudioPart:
    filename: str
    content_type: str
    data: bytes


class _DelegatedChatRequest:
    """Request view that hands a bounded transcript to the native chat route."""

    def __init__(self, request: Request, payload: dict[str, object]):
        self.scope = request.scope
        # The delegated body is the small JSON transcript, not the original
        # multipart upload.  Do not let the upload's content-length trip the
        # native chat route's 64 KiB JSON guard.
        self.headers = dict(request.headers)
        self.headers.pop("content-length", None)
        self.headers["content-type"] = "application/json"
        self.query_params = request.query_params
        self._payload = payload

    async def json(self) -> dict[str, object]:
        return self._payload


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _failure(
    status_code: int,
    *,
    error: str,
    outcome: str,
    retryable: bool,
    message: str,
) -> JSONResponse:
    return JSONResponse(
        {
            "detail": {
                "error": error,
                "outcome": outcome,
                "provider": "workers-ai",
                "retryable": retryable,
                "message": message,
            }
        },
        status_code=status_code,
    )


def _invalid_input(message: str = "The audio input is invalid.", *, status_code: int = 400) -> JSONResponse:
    return _failure(
        status_code,
        error="stt_invalid_input",
        outcome="invalid_input",
        retryable=False,
        message=message,
    )


def _header_parameter(value: str, header: str, name: str) -> str | None:
    message = Message()
    message[header] = value
    result = message.get_param(name, header=header)
    return result if isinstance(result, str) else None


def _multipart_boundary(content_type: str) -> bytes:
    boundary = _header_parameter(content_type, "content-type", "boundary")
    if boundary is None or not 1 <= len(boundary) <= 70:
        raise ValueError("missing multipart boundary")
    try:
        encoded = boundary.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("invalid multipart boundary") from error
    if any(byte <= 32 or byte >= 127 for byte in encoded):
        raise ValueError("invalid multipart boundary")
    return encoded


def _parse_multipart(body: bytes, content_type: str) -> tuple[list[AudioPart], str | None]:
    boundary = _multipart_boundary(content_type)
    delimiter = b"--" + boundary
    if not body.startswith(delimiter):
        raise ValueError("malformed multipart body")
    chunks = body.split(delimiter)
    if chunks[0] or len(chunks) < 3:
        raise ValueError("malformed multipart body")

    files: list[AudioPart] = []
    language: str | None = None
    part_count = 0
    closed = False
    for index, chunk in enumerate(chunks[1:]):
        if chunk.startswith(b"--"):
            if index != len(chunks) - 2 or chunk[2:].strip(b"\r\n"):
                raise ValueError("malformed multipart terminator")
            closed = True
            break
        if not chunk.startswith(b"\r\n") or not chunk.endswith(b"\r\n"):
            raise ValueError("malformed multipart part")
        part_count += 1
        if part_count > MAX_MULTIPART_PARTS:
            raise ValueError("too many multipart parts")
        raw_part = chunk[2:-2]
        header_bytes, separator, data = raw_part.partition(b"\r\n\r\n")
        if not separator or len(header_bytes) > MAX_MULTIPART_HEADERS_BYTES:
            raise ValueError("malformed multipart headers")
        try:
            headers = BytesHeaderParser(policy=email_policy).parsebytes(header_bytes + b"\r\n\r\n")
        except (TypeError, ValueError) as error:
            raise ValueError("malformed multipart headers") from error
        disposition = headers.get("content-disposition", "")
        if not disposition.lower().startswith("form-data"):
            raise ValueError("invalid multipart disposition")
        name = _header_parameter(disposition, "content-disposition", "name")
        filename = _header_parameter(disposition, "content-disposition", "filename")
        if name == "files":
            if not filename:
                raise ValueError("missing audio filename")
            files.append(
                AudioPart(
                    filename=filename,
                    content_type=headers.get_content_type() if headers.get("content-type") else "",
                    data=data,
                )
            )
            if len(files) > MAX_MULTIPART_FILES:
                raise ValueError("too many audio files")
        elif name == "language":
            if language is not None or len(data) > MAX_LANGUAGE_CHARS:
                raise ValueError("invalid language field")
            try:
                language = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("invalid language field") from error
    if not closed or not files:
        raise ValueError("no audio files provided")
    return files, language


def _normalize_language(raw: str | None) -> str | None:
    if raw is None:
        return None
    normalized = raw.strip()
    if not normalized or normalized.lower() in {"auto", "multi"}:
        return None
    if len(normalized) > MAX_LANGUAGE_CHARS or _LANGUAGE_PATTERN.fullmatch(normalized) is None:
        raise ValueError("invalid transcription language")
    return normalized.replace("_", "-").lower()


def _validate_audio_part(part: AudioPart) -> None:
    if not part.data:
        raise ValueError("empty audio file")
    if len(part.data) > MAX_AUDIO_BYTES:
        raise OverflowError("audio file too large")
    content_type = part.content_type.split(";", 1)[0].strip().lower()
    if content_type not in _SUPPORTED_AUDIO_MIME_TYPES:
        raise ValueError("unsupported audio content type")
    suffix = part.filename.rsplit(".", 1)[-1].lower() if "." in part.filename else ""
    if suffix == "wav":
        valid = len(part.data) >= 12 and part.data.startswith(b"RIFF") and part.data[8:12] == b"WAVE"
    elif suffix == "webm":
        valid = part.data.startswith(b"\x1a\x45\xdf\xa3")
    elif suffix == "mp4":
        valid = len(part.data) >= 12 and part.data[4:8] == b"ftyp"
    else:
        raise ValueError("unsupported audio filename")
    if not valid:
        raise ValueError("invalid audio container")


def _pcm_wav(pcm: bytes, *, sample_rate: int, channels: int) -> bytes:
    if not pcm or len(pcm) % (channels * 2):
        raise ValueError("invalid linear16 PCM body")
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    return b"".join(
        (
            b"RIFF",
            struct.pack("<I", len(pcm) + 36),
            b"WAVEfmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, 16),
            b"data",
            struct.pack("<I", len(pcm)),
            pcm,
        )
    )


def _workers_ai_mapping(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    to_py = getattr(value, "to_py", None)
    if callable(to_py):
        converted = to_py()
        if isinstance(converted, dict):
            return converted
    return None


def _detected_language(payload: dict[str, object]) -> str | None:
    detected = payload.get("detected_language")
    info = payload.get("transcription_info")
    if not isinstance(detected, str) and isinstance(info, dict):
        detected = info.get("language")
    return detected.strip() if isinstance(detected, str) and detected.strip() else None


async def _transcribe_parts(ai: object, model: str, parts: list[AudioPart], language: str | None):
    transcripts: list[str] = []
    detected_languages: set[str] = set()
    speech_ms = 0
    for part in parts:
        request_payload: dict[str, object] = {
            "audio": base64.b64encode(part.data).decode("ascii"),
            "task": "transcribe",
            "vad_filter": True,
        }
        if language:
            request_payload["language"] = language
        result = _workers_ai_mapping(await ai.run(model, request_payload))
        if result is None or not isinstance(result.get("text"), str):
            raise TypeError("invalid Workers AI transcription")
        transcript = str(result["text"]).strip()
        if transcript:
            transcripts.append(transcript)
        detected = _detected_language(result)
        if detected:
            detected_languages.add(detected)
        speech_ms += speech_ms_from_transcription(result)
    detected_language = None
    if len(detected_languages) == 1:
        detected_language = next(iter(detected_languages))
    elif len(detected_languages) > 1:
        detected_language = "multi"
    return " ".join(transcripts), detected_language, speech_ms


@router.post("/v2/voice-message/transcribe")
async def transcribe_voice_message(request: Request):
    """Transcribe the Web/Flutter upload contract with native Workers AI."""
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    restriction = await fair_use_restriction(request.scope["env"], str(context["uid"]))
    if restriction:
        return fair_use_restriction_response(restriction)

    content_type = request.headers.get("content-type", "").strip()
    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
    max_body_bytes = MAX_MULTIPART_BODY_BYTES if normalized_content_type == "multipart/form-data" else MAX_AUDIO_BYTES
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_body_bytes:
                return _invalid_input("The audio body is too large.", status_code=413)
        except ValueError:
            return _invalid_input()
    body = await request.body()
    if len(body) > max_body_bytes:
        return _invalid_input("The audio body is too large.", status_code=413)

    try:
        if normalized_content_type == "multipart/form-data":
            parts, form_language = _parse_multipart(body, content_type)
            for part in parts:
                _validate_audio_part(part)
            language = _normalize_language(form_language)
        elif normalized_content_type == "application/octet-stream":
            encoding = request.query_params.get("encoding", "linear16").strip().lower()
            if encoding not in {"linear16", "pcm_s16le"}:
                raise ValueError("unsupported PCM encoding")
            sample_rate = int(request.query_params.get("sample_rate", "16000"))
            channels = int(request.query_params.get("channels", "1"))
            if not 8_000 <= sample_rate <= 48_000 or not 1 <= channels <= 2:
                raise ValueError("invalid PCM format")
            language = _normalize_language(request.query_params.get("language"))
            audio = _pcm_wav(body, sample_rate=sample_rate, channels=channels)
            if len(audio) > MAX_AUDIO_BYTES:
                raise OverflowError("audio body too large")
            parts = [AudioPart(filename="audio.wav", content_type="audio/wav", data=audio)]
        else:
            return _invalid_input("Unsupported transcription content type.", status_code=415)
    except OverflowError:
        return _invalid_input("The audio body is too large.", status_code=413)
    except (TypeError, ValueError):
        return _invalid_input()

    env = request.scope["env"]
    ai = getattr(env, "AI", None)
    if ai is None:
        return _failure(
            503,
            error="stt_provider_configuration_error",
            outcome="config_error",
            retryable=False,
            message="The transcription provider is temporarily unavailable.",
        )
    model = getattr(env, "WORKERS_AI_ASR_MODEL", DEFAULT_WORKERS_AI_ASR_MODEL)
    try:
        transcript, detected_language, speech_ms = await _transcribe_parts(ai, model, parts, language)
    except Exception:
        return _failure(
            502,
            error="stt_upstream_error",
            outcome="upstream_error",
            retryable=True,
            message="The transcription provider could not complete the request.",
        )

    try:
        await record_fair_use_usage(
            env,
            uid=str(context["uid"]),
            source_kind="sync_fresh",
            source_id=content_source_id(
                "voice-message",
                body,
                request.headers.get("idempotency-key")
                or (str(context["requestId"]) if isinstance(context.get("requestId"), str) else None),
            ),
            speech_ms=speech_ms,
        )
    except Exception:
        return _failure(
            503,
            error="stt_meter_unavailable",
            outcome="dependency_error",
            retryable=True,
            message="The transcription usage meter is temporarily unavailable.",
        )

    response: dict[str, object] = {
        "transcript": transcript,
        "stt_provider": "workers-ai",
        "stt_model": model,
        "outcome": "success" if transcript else "expected_silence",
    }
    if detected_language:
        response["language"] = detected_language
    return response


@router.post("/v2/voice-messages")
async def create_voice_message_stream(request: Request):
    """Transcribe a voice message with Workers AI, then stream native chat.

    The legacy endpoint combined local audio decoding, STT, and chat writes in
    one process. Cloudflare keeps the multipart/SSE boundary by using the
    bounded native STT parser and delegating the transcript to the D1-backed
    ``/v2/messages`` implementation. Empty/silent input emits an empty SSE
    stream, matching the legacy no-answer behavior.
    """

    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    restriction = await fair_use_restriction(request.scope["env"], str(context["uid"]))
    if restriction:
        return fair_use_restriction_response(restriction)

    content_type = request.headers.get("content-type", "").strip()
    if content_type.split(";", 1)[0].strip().lower() != "multipart/form-data":
        return _invalid_input("Voice messages require multipart/form-data.", status_code=415)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_MULTIPART_BODY_BYTES:
                return _invalid_input("The audio body is too large.", status_code=413)
        except ValueError:
            return _invalid_input()
    body = await request.body()
    if len(body) > MAX_MULTIPART_BODY_BYTES:
        return _invalid_input("The audio body is too large.", status_code=413)

    try:
        parts, form_language = _parse_multipart(body, content_type)
        for part in parts:
            _validate_audio_part(part)
        language = _normalize_language(form_language)
    except OverflowError:
        return _invalid_input("The audio body is too large.", status_code=413)
    except (TypeError, ValueError):
        return _invalid_input()

    env = request.scope["env"]
    ai = getattr(env, "AI", None)
    if ai is None:
        return _failure(
            503,
            error="stt_provider_configuration_error",
            outcome="config_error",
            retryable=False,
            message="The transcription provider is temporarily unavailable.",
        )
    model = getattr(env, "WORKERS_AI_ASR_MODEL", DEFAULT_WORKERS_AI_ASR_MODEL)
    try:
        transcript, _detected_language, speech_ms = await _transcribe_parts(ai, model, parts, language)
    except Exception:
        return _failure(
            502,
            error="stt_upstream_error",
            outcome="upstream_error",
            retryable=True,
            message="The transcription provider could not complete the request.",
        )

    try:
        await record_fair_use_usage(
            env,
            uid=str(context["uid"]),
            source_kind="sync_fresh",
            source_id=content_source_id(
                "voice-message-chat",
                body,
                request.headers.get("idempotency-key")
                or (str(context["requestId"]) if isinstance(context.get("requestId"), str) else None),
            ),
            speech_ms=speech_ms,
        )
    except Exception:
        return _failure(
            503,
            error="stt_meter_unavailable",
            outcome="dependency_error",
            retryable=True,
            message="The transcription usage meter is temporarily unavailable.",
        )

    if not transcript:
        return StreamingResponse(iter(()), media_type="text/event-stream")

    # Imported lazily to keep the standalone STT module usable in CPython tests
    # and to avoid an import cycle during API AI Worker startup.
    from chat_generation_routes import chat_messages

    return await chat_messages(_DelegatedChatRequest(request, {"text": transcript}))


__all__ = ["create_voice_message_stream", "router", "transcribe_voice_message"]
