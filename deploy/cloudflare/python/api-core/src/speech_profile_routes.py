"""R2-owned speech-profile routes for the isolated Cloudflare profile.

The profile blob and its duration metadata are committed in one R2 put. A
native Workers AI transcription call is the fail-closed speech-presence gate;
the Worker never loads a local VAD, ASR, or speaker model.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
from email.message import Message
from email.parser import BytesHeaderParser
from email.policy import default as email_policy
from typing import NamedTuple
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from internal_auth import decode_context

router = APIRouter()

MAX_MULTIPART_BODY_BYTES = 50 * 1024 * 1024 + 64 * 1024
MAX_MULTIPART_HEADERS_BYTES = 8 * 1024
MAX_MULTIPART_PARTS = 8
MAX_PROFILE_AUDIO_BYTES = 50 * 1024 * 1024
MAX_ID_LENGTH = 256
MAX_SAMPLE_COUNT = 1_000
PROFILE_URL_TTL_SECONDS = 60
DEFAULT_WORKERS_AI_ASR_MODEL = "@cf/openai/whisper-large-v3-turbo"
PROFILE_KEY_SUFFIX = "speech_profile.wav"
ADDITIONAL_SAMPLE_PREFIX = "additional_profile_recordings"
PEOPLE_SAMPLE_PREFIX = "people_profiles"


class AudioPart(NamedTuple):
    filename: str
    content_type: str
    data: bytes


class WavInfo(NamedTuple):
    duration_seconds: float
    channels: int
    bits_per_sample: int


def _auth_context(request: Request) -> dict[str, object] | None:
    env = request.scope["env"]
    return decode_context(
        request.headers.get("x-omi-auth-context"),
        request.headers.get("x-omi-internal-signature"),
        getattr(env, "INTERNAL_ASSERTION_SECRET", None),
    )


def _bucket(env: object) -> object | None:
    bucket = getattr(env, "SPEECH_PROFILES", None)
    if bucket is None or not all(
        callable(getattr(bucket, method, None)) for method in ("head", "get", "put", "list", "delete")
    ):
        return None
    return bucket


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


def _parse_profile_multipart(body: bytes, content_type: str) -> AudioPart:
    boundary = _multipart_boundary(content_type)
    delimiter = b"--" + boundary
    if not body.startswith(delimiter):
        raise ValueError("malformed multipart body")
    chunks = body.split(delimiter)
    if chunks[0] or len(chunks) < 3:
        raise ValueError("malformed multipart body")

    selected: AudioPart | None = None
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
        headers = BytesHeaderParser(policy=email_policy).parsebytes(header_bytes + b"\r\n\r\n")
        disposition = headers.get("content-disposition", "")
        if not disposition.lower().startswith("form-data"):
            raise ValueError("invalid multipart disposition")
        name = _header_parameter(disposition, "content-disposition", "name")
        filename = _header_parameter(disposition, "content-disposition", "filename")
        if name != "file":
            continue
        if selected is not None or not filename:
            raise ValueError("invalid audio file field")
        selected = AudioPart(
            filename=filename,
            content_type=headers.get_content_type() if headers.get("content-type") else "",
            data=data,
        )
    if not closed or selected is None:
        raise ValueError("no audio file provided")
    if not selected.data:
        raise ValueError("empty audio file")
    if len(selected.data) > MAX_PROFILE_AUDIO_BYTES:
        raise OverflowError("audio file too large")
    return selected


def _little_uint(data: bytes, offset: int, size: int) -> int:
    if offset < 0 or offset + size > len(data):
        raise ValueError("truncated wav")
    return int.from_bytes(data[offset : offset + size], "little", signed=False)


def _wav_info(data: bytes) -> WavInfo:
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("invalid wav")
    declared_size = _little_uint(data, 4, 4) + 8
    if declared_size > len(data):
        raise ValueError("truncated wav")
    offset = 12
    audio_format = channels = sample_rate = byte_rate = block_align = bits_per_sample = None
    data_size = None
    while offset + 8 <= declared_size:
        chunk_id = data[offset : offset + 4]
        chunk_size = _little_uint(data, offset + 4, 4)
        start = offset + 8
        end = start + chunk_size
        if end > declared_size:
            raise ValueError("truncated wav chunk")
        if chunk_id == b"fmt " and chunk_size >= 16:
            audio_format = _little_uint(data, start, 2)
            channels = _little_uint(data, start + 2, 2)
            sample_rate = _little_uint(data, start + 4, 4)
            byte_rate = _little_uint(data, start + 8, 4)
            block_align = _little_uint(data, start + 12, 2)
            bits_per_sample = _little_uint(data, start + 14, 2)
        elif chunk_id == b"data" and data_size is None:
            data_size = chunk_size
        offset = end + (chunk_size % 2)
    if None in (audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample, data_size):
        raise ValueError("incomplete wav")
    if audio_format != 1 or not 1 <= channels <= 8 or bits_per_sample not in {8, 16, 24, 32}:
        raise ValueError("unsupported wav codec")
    expected_align = channels * (bits_per_sample // 8)
    if block_align != expected_align or byte_rate != sample_rate * block_align or data_size % block_align:
        raise ValueError("invalid wav layout")
    if sample_rate != 16_000:
        raise LookupError("invalid sample rate")
    duration = data_size / byte_rate
    if not math.isfinite(duration) or duration < 5 or duration > 120:
        raise ArithmeticError("invalid duration")
    return WavInfo(duration_seconds=duration, channels=channels, bits_per_sample=bits_per_sample)


def _to_python(value: object, *, allow_to_py: bool = True) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _to_python(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_python(item) for item in value]
    if allow_to_py:
        try:
            binding = object.__getattribute__(value, "_binding")
        except (AttributeError, TypeError):
            binding = value
        to_py = getattr(binding, "to_py", None)
        try:
            converted = to_py() if callable(to_py) else None
        except (TypeError, ValueError):
            converted = None
        if converted is not None and converted is not binding:
            # Python Workers wraps R2 RPC results. Calling the wrapper's
            # dynamically exposed ``to_py`` can return a fresh wrapper around
            # the same JS object forever, so unwrap the binding once and do not
            # invoke conversion recursively on the result.
            return _to_python(converted, allow_to_py=False)
    result: dict[str, object] = {}
    for field in (
        "text",
        "segments",
        "transcription_info",
        "detected_language",
        "start",
        "end",
        "no_speech_prob",
        "objects",
        "cursor",
        "truncated",
        "key",
        "customMetadata",
        "size",
        "etag",
    ):
        item = getattr(value, field, None)
        if item is not None:
            result[field] = _to_python(item)
    return result


def _transcription_payload(value: object) -> dict[str, object] | None:
    converted = _to_python(value)
    return converted if isinstance(converted, dict) else None


def _speech_segments(payload: dict[str, object]) -> list[tuple[float, float]]:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        return []
    segments: list[tuple[float, float]] = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            continue
        start = raw.get("start")
        end = raw.get("end")
        no_speech = raw.get("no_speech_prob")
        if (
            isinstance(start, (int, float))
            and not isinstance(start, bool)
            and isinstance(end, (int, float))
            and not isinstance(end, bool)
            and math.isfinite(float(start))
            and math.isfinite(float(end))
            and float(end) > float(start)
            and (not isinstance(no_speech, (int, float)) or isinstance(no_speech, bool) or float(no_speech) < 0.8)
        ):
            segments.append((max(0.0, float(start)), float(end)))
    return segments


def _has_speech(payload: dict[str, object]) -> bool:
    text = payload.get("text")
    return (isinstance(text, str) and bool(text.strip())) or bool(_speech_segments(payload))


def _profile_duration(payload: dict[str, object], source_duration: float) -> float:
    segments = sorted(_speech_segments(payload))
    if not segments:
        return source_duration + 5
    merged: list[list[float]] = []
    for start, end in segments:
        bounded_end = min(source_duration, end)
        if bounded_end <= start:
            continue
        if merged and start - merged[-1][1] < 1:
            merged[-1][1] = max(merged[-1][1], bounded_end)
        else:
            merged.append([start, bounded_end])
    speech_duration = sum(end - start for start, end in merged)
    return speech_duration + 5 if speech_duration > 0 else source_duration + 5


def _signing_secret(env: object) -> str | None:
    value = getattr(env, "SPEECH_PROFILE_URL_SIGNING_SECRET", None) or getattr(env, "INTERNAL_ASSERTION_SECRET", None)
    return value if isinstance(value, str) and len(value) >= 16 else None


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _profile_token(env: object, uid: str, key: str, expires_at: int) -> str | None:
    secret = _signing_secret(env)
    if secret is None:
        return None
    payload = json.dumps({"e": expires_at, "k": key, "u": uid, "v": 1}, separators=(",", ":"), sort_keys=True).encode()
    encoded = _b64url(payload)
    signature = _b64url(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def _valid_object_key(uid: str, key: str) -> bool:
    prefix = f"{uid}/"
    if not key.startswith(prefix) or len(key) > 1_024 or "\x00" in key or "\\" in key:
        return False
    parts = key.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    return key == f"{uid}/{PROFILE_KEY_SUFFIX}" or key.startswith(
        (f"{uid}/{ADDITIONAL_SAMPLE_PREFIX}/", f"{uid}/{PEOPLE_SAMPLE_PREFIX}/")
    )


def _token_identity(env: object, token: object, now: int) -> tuple[str, str] | None:
    secret = _signing_secret(env)
    if secret is None or not isinstance(token, str) or len(token) > 2_048 or token.count(".") != 1:
        return None
    encoded, supplied = token.split(".", 1)
    expected = _b64url(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(supplied, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode())
        expires_at = int(payload["e"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    uid = payload.get("u")
    key = payload.get("k")
    if (
        payload.get("v") != 1
        or not isinstance(uid, str)
        or not 0 < len(uid) <= MAX_ID_LENGTH
        or not isinstance(key, str)
        or not _valid_object_key(uid, key)
        or expires_at < now
        or expires_at > now + PROFILE_URL_TTL_SECONDS + 5
    ):
        return None
    return uid, key


def _signed_profile_url(request: Request, env: object, uid: str, key: str) -> str | None:
    token = _profile_token(env, uid, key, int(time.time()) + PROFILE_URL_TTL_SECONDS)
    if token is None:
        return None
    source = urlsplit(str(request.url))
    return urlunsplit((source.scheme, source.netloc, "/v3/speech-profile/audio", urlencode({"token": token}), ""))


def _metadata_mapping(metadata: object) -> dict[str, object]:
    converted = _to_python(metadata)
    if not isinstance(converted, dict):
        return {}
    custom = converted.get("customMetadata")
    return custom if isinstance(custom, dict) else {}


def _metadata_size(metadata: object) -> int:
    converted = _to_python(metadata)
    value = converted.get("size") if isinstance(converted, dict) else None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0


def _valid_id(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= MAX_ID_LENGTH
        and value not in {".", ".."}
        and not any(character in value for character in ("/", "\\", "\x00"))
    )


async def _list_keys(bucket: object, prefix: str) -> list[str]:
    raw = await bucket.list({"prefix": prefix, "limit": MAX_SAMPLE_COUNT})
    payload = _to_python(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("invalid R2 list response")
    objects = payload.get("objects")
    if not isinstance(objects, list):
        raise RuntimeError("invalid R2 object list")
    if bool(payload.get("truncated")) or len(objects) > MAX_SAMPLE_COUNT:
        raise OverflowError("too many speech samples")
    keys = [str(item["key"]) for item in objects if isinstance(item, dict) and isinstance(item.get("key"), str)]
    if len(keys) != len(objects) or any(not key.startswith(prefix) for key in keys):
        raise RuntimeError("invalid R2 object key")
    return sorted(keys)


async def _r2_chunks(stored: object):
    stream = getattr(stored, "body", None)
    get_reader = getattr(stream, "getReader", None)
    if callable(get_reader):
        reader = get_reader()
        try:
            while True:
                result = await reader.read()
                if bool(getattr(result, "done", False)):
                    break
                value = getattr(result, "value", b"")
                to_py = getattr(value, "to_py", None)
                yield bytes(to_py() if callable(to_py) else value)
        finally:
            release_lock = getattr(reader, "releaseLock", None)
            if callable(release_lock):
                release_lock()
        return
    yield bytes(await stored.arrayBuffer())


def _parse_range(raw: str | None, size: int) -> tuple[int, int] | None:
    if not raw:
        return None
    if not raw.startswith("bytes=") or "," in raw or "-" not in raw[6:] or size <= 0:
        raise ValueError("unsupported range")
    start_raw, end_raw = raw[6:].strip().split("-", 1)
    if not start_raw and not end_raw:
        raise ValueError("unsupported range")
    try:
        if not start_raw:
            suffix = int(end_raw)
            if suffix <= 0:
                raise ValueError("invalid suffix")
            return max(0, size - suffix), size - 1
        start = int(start_raw)
        end = size - 1 if not end_raw else min(size - 1, int(end_raw))
    except ValueError as error:
        raise ValueError("invalid range") from error
    if start < 0 or start >= size or end < start:
        raise ValueError("unsatisfiable range")
    return start, end


@router.get("/v3/speech-profile")
async def has_speech_profile(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    bucket = _bucket(request.scope["env"])
    if bucket is None:
        return JSONResponse({"error": "speech profile storage is not configured"}, status_code=503)
    try:
        profile = await bucket.head(f"{context['uid']}/{PROFILE_KEY_SUFFIX}")
    except Exception:
        return JSONResponse({"error": "speech profile storage is unavailable"}, status_code=503)
    return {"has_profile": profile is not None}


@router.get("/v4/speech-profile")
async def get_speech_profile(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    bucket = _bucket(env)
    if bucket is None:
        return JSONResponse({"error": "speech profile storage is not configured"}, status_code=503)
    uid = str(context["uid"])
    key = f"{uid}/{PROFILE_KEY_SUFFIX}"
    try:
        profile = await bucket.head(key)
    except Exception:
        return JSONResponse({"error": "speech profile storage is unavailable"}, status_code=503)
    if profile is None:
        return {"url": None}
    url = _signed_profile_url(request, env, uid, key)
    if url is None:
        return JSONResponse({"error": "speech profile URL signing is not configured"}, status_code=503)
    return {"url": url}


@router.get("/v3/speech-profile/status")
async def get_speech_profile_status(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    env = request.scope["env"]
    bucket = _bucket(env)
    if bucket is None:
        return JSONResponse({"error": "speech profile storage is not configured"}, status_code=503)
    uid = str(context["uid"])
    key = f"{uid}/{PROFILE_KEY_SUFFIX}"
    try:
        profile = await bucket.head(key)
        sample_keys = await _list_keys(bucket, f"{uid}/{ADDITIONAL_SAMPLE_PREFIX}/")
    except OverflowError:
        return JSONResponse({"error": "too many speech profile samples"}, status_code=409)
    except Exception:
        return JSONResponse({"error": "speech profile storage is unavailable"}, status_code=503)
    if profile is None:
        return {"has_profile": False, "duration_seconds": 0.0, "sample_count": len(sample_keys), "url": None}
    metadata = _metadata_mapping(profile)
    try:
        duration = float(metadata.get("duration_seconds", 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration) or duration < 0:
        duration = 0.0
    url = _signed_profile_url(request, env, uid, key)
    if url is None:
        return JSONResponse({"error": "speech profile URL signing is not configured"}, status_code=503)
    return {"has_profile": True, "duration_seconds": duration, "sample_count": len(sample_keys), "url": url}


@router.post("/v3/upload-audio")
async def upload_profile(request: Request):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "multipart/form-data":
        return JSONResponse({"detail": "Invalid audio file: must be a valid 16kHz WAV."}, status_code=400)
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > MAX_MULTIPART_BODY_BYTES:
            return JSONResponse({"detail": "Audio file is too large"}, status_code=413)
    except ValueError:
        return JSONResponse({"detail": "Invalid audio file: must be a valid 16kHz WAV."}, status_code=400)
    body = await request.body()
    if len(body) > MAX_MULTIPART_BODY_BYTES:
        return JSONResponse({"detail": "Audio file is too large"}, status_code=413)
    try:
        part = _parse_profile_multipart(body, content_type)
        wav = _wav_info(part.data)
    except OverflowError:
        return JSONResponse({"detail": "Audio file is too large"}, status_code=413)
    except LookupError:
        return JSONResponse({"detail": "Invalid codec, must be opus 16khz."}, status_code=400)
    except ArithmeticError:
        return JSONResponse({"detail": "Audio duration is invalid (must be 5-120 seconds)"}, status_code=400)
    except (TypeError, ValueError):
        return JSONResponse({"detail": "Invalid audio file: must be a valid 16kHz WAV."}, status_code=400)

    env = request.scope["env"]
    bucket = _bucket(env)
    ai = getattr(env, "AI", None)
    if bucket is None:
        return JSONResponse({"error": "speech profile storage is not configured"}, status_code=503)
    if ai is None:
        return JSONResponse({"error": "speech validation is not configured"}, status_code=503)
    uid = str(context["uid"])
    key = f"{uid}/{PROFILE_KEY_SUFFIX}"
    url = _signed_profile_url(request, env, uid, key)
    if url is None:
        return JSONResponse({"error": "speech profile URL signing is not configured"}, status_code=503)
    model = getattr(env, "WORKERS_AI_ASR_MODEL", DEFAULT_WORKERS_AI_ASR_MODEL)
    try:
        result = await ai.run(
            model,
            {
                "audio": base64.b64encode(part.data).decode("ascii"),
                "vad_filter": True,
                "condition_on_previous_text": False,
                "no_speech_threshold": 0.5,
                "hallucination_silence_threshold": 1.0,
            },
        )
        payload = _transcription_payload(result)
    except Exception:
        return JSONResponse({"error": "speech validation is unavailable"}, status_code=502)
    if payload is None:
        return JSONResponse({"error": "speech validation returned an invalid response"}, status_code=502)
    if not _has_speech(payload):
        return JSONResponse({"detail": "Audio is empty"}, status_code=400)
    duration = _profile_duration(payload, wav.duration_seconds)
    try:
        await bucket.put(
            key,
            part.data,
            httpMetadata={"contentType": "audio/wav"},
            customMetadata={
                "duration_seconds": f"{duration:.6f}",
                "channels": str(wav.channels),
                "bits_per_sample": str(wav.bits_per_sample),
                "validation_model": str(model)[:256],
            },
        )
    except Exception:
        return JSONResponse({"error": "speech profile storage is unavailable"}, status_code=503)
    return {"url": url}


@router.get("/v3/speech-profile/expand")
async def get_extra_speech_profile_samples(request: Request, person_id: str | None = None):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if person_id is not None and not _valid_id(person_id):
        return JSONResponse({"error": "invalid person id"}, status_code=400)
    env = request.scope["env"]
    bucket = _bucket(env)
    if bucket is None:
        return JSONResponse({"error": "speech profile storage is not configured"}, status_code=503)
    uid = str(context["uid"])
    prefix = f"{uid}/{PEOPLE_SAMPLE_PREFIX}/{person_id}/" if person_id else f"{uid}/{ADDITIONAL_SAMPLE_PREFIX}/"
    try:
        keys = await _list_keys(bucket, prefix)
    except OverflowError:
        return JSONResponse({"error": "too many speech profile samples"}, status_code=409)
    except Exception:
        return JSONResponse({"error": "speech profile storage is unavailable"}, status_code=503)
    urls = [_signed_profile_url(request, env, uid, key) for key in keys]
    if any(url is None for url in urls):
        return JSONResponse({"error": "speech profile URL signing is not configured"}, status_code=503)
    return urls


@router.delete("/v3/speech-profile/expand")
async def delete_extra_speech_profile_sample(
    request: Request,
    memory_id: str,
    segment_idx: int,
    person_id: str | None = None,
):
    context = _auth_context(request)
    if not context:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _valid_id(memory_id) or person_id not in {None, "null"} and not _valid_id(str(person_id)):
        return JSONResponse({"error": "invalid speech sample identity"}, status_code=400)
    env = request.scope["env"]
    bucket = _bucket(env)
    if bucket is None:
        return JSONResponse({"error": "speech profile storage is not configured"}, status_code=503)
    uid = str(context["uid"])
    filename = f"{memory_id}_segment_{segment_idx}.wav"
    normalized_person_id = None if person_id in {None, "null"} else str(person_id)
    key = (
        f"{uid}/{PEOPLE_SAMPLE_PREFIX}/{normalized_person_id}/{filename}"
        if normalized_person_id
        else f"{uid}/{ADDITIONAL_SAMPLE_PREFIX}/{filename}"
    )
    try:
        await bucket.delete(key)
    except Exception:
        return JSONResponse({"error": "speech profile storage is unavailable"}, status_code=503)
    return {"status": "ok"}


@router.get("/v3/speech-profile/audio")
async def download_speech_profile_audio(request: Request):
    env = request.scope["env"]
    identity = _token_identity(env, request.query_params.get("token"), int(time.time()))
    if identity is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    bucket = _bucket(env)
    if bucket is None:
        return JSONResponse({"error": "speech profile storage is not configured"}, status_code=503)
    _, key = identity
    try:
        metadata = await bucket.head(key)
        if metadata is None:
            return JSONResponse({"error": "speech profile audio not found"}, status_code=404)
        size = _metadata_size(metadata)
        byte_range = _parse_range(request.headers.get("range"), size)
        options = (
            {"range": {"offset": byte_range[0], "length": byte_range[1] - byte_range[0] + 1}} if byte_range else None
        )
        stored = await bucket.get(key, options) if options else await bucket.get(key)
        if stored is None:
            return JSONResponse({"error": "speech profile audio not found"}, status_code=404)
    except ValueError:
        return Response(status_code=416, headers={"content-range": f"bytes */{size}", "accept-ranges": "bytes"})
    except Exception:
        return JSONResponse({"error": "speech profile storage is unavailable"}, status_code=503)
    headers = {"accept-ranges": "bytes", "cache-control": "private, max-age=60"}
    status_code = 200
    if byte_range:
        headers["content-range"] = f"bytes {byte_range[0]}-{byte_range[1]}/{size}"
        headers["content-length"] = str(byte_range[1] - byte_range[0] + 1)
        status_code = 206
    elif size:
        headers["content-length"] = str(size)
    return StreamingResponse(_r2_chunks(stored), media_type="audio/wav", headers=headers, status_code=status_code)


__all__ = [
    "delete_extra_speech_profile_sample",
    "download_speech_profile_audio",
    "get_extra_speech_profile_samples",
    "get_speech_profile",
    "get_speech_profile_status",
    "has_speech_profile",
    "router",
    "upload_profile",
]
