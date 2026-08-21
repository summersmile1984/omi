"""TTS proxy route — proxies ElevenLabs text-to-speech server-side.

Provides the mobile TTS contract so mobile clients can play
Omi's spoken responses in background / lock-screen scenarios without shipping
an ElevenLabs API key to the client.

Rate limits per user (Redis-backed sliding-window + daily counter):
  - 50 requests per rolling 60 seconds → 429
  - 10,000 characters per UTC day → 429
  - 5,000 characters per single request (hard cap, 400)
"""

import logging
import os
from typing import Any, Callable, Dict, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from database import redis_db
from models.tts import DEFAULT_VOICE_ID, TtsSynthesizeRequest
from utils.http_client import get_tts_client, get_tts_semaphore
from utils.log_sanitizer import sanitize
from utils.llm.capabilities import ModelCapabilityUnavailableError
from utils.other import endpoints as auth
from utils.executors import run_blocking, critical_executor
from utils.tts_policy import (
    TTS_DISABLED_DETAIL,
    tts_explicitly_disabled,
    tts_official_provider_forbidden_in_neutral,
    tts_provider_missing_in_neutral_deployment,
)
from utils.tts_provider import selected_tts_provider, synthesize_openai_compatible_tts, synthesize_sherpa_tts

logger = logging.getLogger(__name__)

router = APIRouter()

# `utils.other.endpoints.with_rate_limit` has an untyped `auth_dependency`
# parameter; route access through a cast so this strict-checked file sees a
# concrete callable type instead of `Unknown`.
_auth_module = cast(Any, auth)

_TTS_BURST_PER_MINUTE = 50
_TTS_DAILY_CHAR_LIMIT = 10_000
_TTS_BURST_WINDOW_SECS = 60
_TTS_REQUEST_CHAR_LIMIT = 5_000

_ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"


def _is_valid_voice_id(voice_id: str) -> bool:
    """Alphanumeric only, 1-128 chars. Prevents path traversal against the
    ElevenLabs URL template (e.g. `../../history` retargeting `xi-api-key`).
    """
    return 1 <= len(voice_id) <= 128 and voice_id.isalnum()


def _is_safe_voice_label(voice_id: str) -> bool:
    """Bound a provider voice label without imposing ElevenLabs path rules."""

    return 1 <= len(voice_id) <= 128 and not any(ord(character) < 32 for character in voice_id)


def _is_mimo_enabled() -> bool:
    """MiMo-TTS is the active provider when TTS_PROVIDER=mimo and a key is set."""
    return os.getenv("TTS_PROVIDER", "").strip().lower() == "mimo" and bool(os.getenv("MIMO_API_KEY"))


@router.post(
    '/v2/tts/synthesize',
    tags=['tts'],
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "MP3 audio stream.",
            "content": {"audio/mpeg": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def tts_synthesize(
    req: TtsSynthesizeRequest,
    uid: str = Depends(
        cast(Callable[..., str], _auth_module.with_rate_limit(auth.get_current_user_uid, "tts:synthesize"))
    ),
):
    """Proxy a TTS request to the configured provider. Per-user rate limited."""
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")
    if not _is_safe_voice_label(req.voice_id):
        raise HTTPException(status_code=400, detail="invalid voice_id")
    char_count = len(text)
    if char_count > _TTS_REQUEST_CHAR_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"text exceeds maximum length of {_TTS_REQUEST_CHAR_LIMIT} characters",
        )

    if tts_explicitly_disabled():
        raise HTTPException(status_code=503, detail=TTS_DISABLED_DETAIL)
    if tts_provider_missing_in_neutral_deployment():
        error = ModelCapabilityUnavailableError('tts', 'provider_not_configured', retryable=False)
        raise HTTPException(status_code=503, detail=error.as_dict())

    selected_provider = selected_tts_provider()
    if selected_provider not in {'', 'elevenlabs', 'mimo', 'openai_compatible', 'sherpa_onnx'}:
        error = ModelCapabilityUnavailableError('tts', 'unsupported_provider', retryable=False)
        raise HTTPException(status_code=503, detail=error.as_dict())
    if tts_official_provider_forbidden_in_neutral(selected_provider):
        error = ModelCapabilityUnavailableError('tts', 'official_provider_forbidden', retryable=False)
        raise HTTPException(status_code=503, detail=error.as_dict())

    api_key = os.getenv('ELEVENLABS_API_KEY')
    mimo_enabled = selected_provider == 'mimo' and _is_mimo_enabled()
    compatible_enabled = selected_provider == 'openai_compatible'
    sherpa_enabled = selected_provider == 'sherpa_onnx'
    if selected_provider == 'mimo' and not mimo_enabled:
        error = ModelCapabilityUnavailableError('tts', 'mimo_credential_not_configured', retryable=False)
        raise HTTPException(status_code=503, detail=error.as_dict())
    if selected_provider in {'', 'elevenlabs'} and not api_key:
        error = ModelCapabilityUnavailableError('tts', 'elevenlabs_credential_not_configured', retryable=False)
        raise HTTPException(status_code=503, detail=error.as_dict())

    status, retry_after = await run_blocking(
        critical_executor,
        redis_db.check_tts_rate_limit,
        uid,
        char_count=char_count,
        burst_limit=_TTS_BURST_PER_MINUTE,
        burst_window_secs=_TTS_BURST_WINDOW_SECS,
        daily_char_limit=_TTS_DAILY_CHAR_LIMIT,
    )
    if status == 1:
        logger.warning(f"tts_synthesize: burst rate limit exceeded uid={uid}")
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded: too many TTS requests. Try again in 60 seconds.",
            headers={"Retry-After": str(retry_after or _TTS_BURST_WINDOW_SECS)},
        )
    if status == 2:
        logger.warning(f"tts_synthesize: daily character limit exceeded uid={uid}")
        raise HTTPException(
            status_code=429,
            detail="Daily TTS character limit exceeded. Resets at midnight UTC.",
            headers={"Retry-After": str(retry_after or 3600)},
        )
    # status == -1 (Redis error): fail-open intentionally — TTS is best-effort.

    if mimo_enabled:
        # MiMo-TTS: one-shot chat-completions request returning audio bytes.
        # The client request's voice_id is the ElevenLabs one (Sloane); for
        # MiMo we use the MiMo voice (default 冰糖, MIMO_TTS_VOICE) unless the
        # caller explicitly sent a MiMo voice name in voice_id.
        from utils.mimo_pipeline.tts import MimoTTSClient, MimoTTSAPIError

        requested_voice = req.voice_id.strip() if _is_valid_voice_id(req.voice_id) else None
        if requested_voice in (DEFAULT_VOICE_ID,):
            requested_voice = None  # caller left the ElevenLabs default; use MiMo default
        try:
            client = MimoTTSClient()
            audio = await run_blocking(
                critical_executor,
                client.synthesize,
                text,
                voice=requested_voice,
            )
        except MimoTTSAPIError as exc:
            logger.warning(f"tts_synthesize: MiMo TTS failed uid={uid}: {sanitize(str(exc))}")
            raise HTTPException(status_code=502, detail="TTS upstream unavailable")

        async def mimo_stream():
            yield audio

        return StreamingResponse(
            mimo_stream(),
            media_type="audio/wav",
            headers={"Content-Length": str(len(audio))},
        )

    if compatible_enabled:
        try:
            audio = await synthesize_openai_compatible_tts(
                text,
                voice=req.voice_id,
                audio_format=req.output_format,
            )
        except ModelCapabilityUnavailableError as error:
            raise HTTPException(status_code=503, detail=error.as_dict()) from error

        async def compatible_stream():
            yield audio.content

        return StreamingResponse(
            compatible_stream(),
            media_type=audio.media_type,
            headers={"Content-Length": str(len(audio.content))},
        )

    if sherpa_enabled:
        try:
            audio = await synthesize_sherpa_tts(text, audio_format=req.output_format)
        except ModelCapabilityUnavailableError as error:
            raise HTTPException(status_code=503, detail=error.as_dict()) from error

        async def sherpa_stream():
            yield audio.content

        return StreamingResponse(
            sherpa_stream(),
            media_type=audio.media_type,
            headers={"Content-Length": str(len(audio.content))},
        )

    assert api_key is not None
    if not _is_valid_voice_id(req.voice_id):
        raise HTTPException(status_code=400, detail="invalid voice_id")

    body: Dict[str, Any] = {
        "text": text,
        "model_id": req.model_id,
        "output_format": req.output_format,
    }
    if req.voice_settings is not None:
        body["voice_settings"] = req.voice_settings.model_dump(exclude_none=True)

    url = _ELEVENLABS_URL.format(voice_id=req.voice_id)
    headers = {
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
        "xi-api-key": api_key,
    }

    client = get_tts_client()
    semaphore = get_tts_semaphore()

    # Acquire the semaphore and open the upstream request OUTSIDE the generator
    # so we can raise a proper HTTPException before StreamingResponse starts
    # writing headers. The generator releases both on exit.
    try:
        await semaphore.acquire()
        try:
            upstream_cm = client.stream("POST", url, json=body, headers=headers, timeout=60.0)
            resp = await upstream_cm.__aenter__()
        except httpx.HTTPError as e:
            semaphore.release()
            logger.error(f"tts_synthesize: upstream request failed uid={uid}: {sanitize(str(e))}")
            raise HTTPException(status_code=502, detail="TTS upstream unavailable")

        if resp.status_code >= 400:
            err_body = await resp.aread()
            err_text = err_body.decode('utf-8', errors='replace')[:200]
            await upstream_cm.__aexit__(None, None, None)
            semaphore.release()
            logger.warning(
                f"tts_synthesize: ElevenLabs returned {resp.status_code} uid={uid}: " f"{sanitize(err_text)}"
            )
            raise HTTPException(status_code=resp.status_code, detail="TTS upstream error")
    except HTTPException:
        raise
    except Exception as e:
        # Defensive: never leak the semaphore on an unexpected failure above.
        try:
            semaphore.release()
        except Exception:
            pass
        logger.error(f"tts_synthesize: pre-stream failure uid={uid}: {sanitize(str(e))}")
        raise HTTPException(status_code=502, detail="TTS upstream unavailable")

    async def audio_stream():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            try:
                await upstream_cm.__aexit__(None, None, None)
            except Exception:
                pass
            try:
                semaphore.release()
            except Exception:
                pass

    return StreamingResponse(audio_stream(), media_type="audio/mpeg")
