"""Existing authenticated HTTP/WS wire contracts backed by the local speech owner."""

from __future__ import annotations

import asyncio
import contextlib
import re
import subprocess
from time import monotonic

from fastapi import HTTPException
from starlette.responses import Response
from starlette.websockets import WebSocketDisconnect
from utils.observability.fallback import record_fallback

from . import speech
from .speech import SpeechError


def voice_for_request(request):
    requested = getattr(request, 'voice_id', '').strip()
    if requested in speech.TTS_VOICES:
        return requested
    # Defaults sent by already-shipped clients select this deployment's default
    # voice. Arbitrary vendor voice/model settings are not silently accepted.
    if requested not in {'', 'default', 'alloy', 'BAMYoBHLZM7lJgJAmFz0'}:
        raise SpeechError('speech_unsupported_voice')
    return 'zf_xiaobei' if re.search(r'[\u3400-\u9fff]', request.text) else 'af_heart'


def _synthesize(request):
    if getattr(request, 'instructions', None) or getattr(request, 'voice_settings', None):
        raise SpeechError('speech_unsupported_settings')
    if getattr(request, 'model_id', 'eleven_turbo_v2_5') not in ('eleven_turbo_v2_5', 'kokoro'):
        raise SpeechError('speech_unsupported_model')
    format_name = getattr(request, 'output_format', 'mp3_44100_128')
    if format_name not in ('wav', 'mp3_44100_128'):
        raise SpeechError('speech_unsupported_format')
    audio = speech.runtime().synthesize(request.text.strip(), voice_for_request(request))
    if format_name == 'wav':
        return audio, 'audio/wav'
    try:
        result = subprocess.run(
            [
                'ffmpeg',
                '-v',
                'error',
                '-nostdin',
                '-i',
                'pipe:0',
                '-f',
                'mp3',
                '-ar',
                '44100',
                '-b:a',
                '128k',
                'pipe:1',
            ],
            input=audio,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SpeechError('speech_audio_encoding_failed', retryable=True) from error
    if not result.stdout or len(result.stdout) > 2_000_000:
        raise SpeechError('speech_invalid_audio_result', retryable=True)
    return result.stdout, 'audio/mpeg'


async def tts(request, uid):
    from database.redis_db import check_tts_rate_limit
    from utils.executors import critical_executor, sync_executor, run_blocking

    if not request.text.strip() or len(request.text) > speech.MAX_TEXT_CHARACTERS:
        raise HTTPException(400, detail={'code': 'speech_invalid_input', 'retryable': False})
    status, retry_after = await run_blocking(
        critical_executor,
        check_tts_rate_limit,
        uid,
        char_count=len(request.text),
        burst_limit=20,
        daily_char_limit=50000,
    )
    if status == -1:
        raise HTTPException(503, detail={'code': 'speech_rate_limit_unavailable', 'retryable': True})
    if status in (1, 2):
        raise HTTPException(
            429,
            detail={'code': 'speech_rate_limited', 'retryable': True},
            headers={'Retry-After': str(retry_after or 60)},
        )
    try:
        audio, media_type = await run_blocking(sync_executor, _synthesize, request)
    except SpeechError as error:
        invalid = error.code in {
            'speech_invalid_input',
            'speech_unsupported_voice',
            'speech_unsupported_settings',
            'speech_unsupported_model',
            'speech_unsupported_format',
        }
        raise HTTPException(
            400 if invalid else 503, detail={'code': error.code, 'retryable': error.retryable}
        ) from error
    return Response(audio, media_type=media_type)


async def tts_mobile(req, uid):
    return await tts(req, uid)


async def prerecorded(request, uid, x_app_platform=None):
    from routers import chat
    from utils.stt.outcomes import TranscriptionFailure

    try:
        return await chat.transcribe_voice_message(request, uid, x_app_platform)
    except TranscriptionFailure as failure:
        # The upstream selector runs before its provider try/except. Local
        # language/disabled admission must retain that same typed HTTP contract.
        raise chat._transcription_http_error(failure) from failure


async def ptt(
    websocket, uid, language='en', sample_rate=16000, codec='linear16', channels=1, keywords=None, x_app_platform=None
):
    from routers import chat
    from utils.executors import critical_executor, db_executor, run_blocking
    from utils.sensevoice.socket import SenseVoiceSocket
    from utils.stt.outcomes import TranscriptionFailure

    await websocket.accept()
    try:
        speech.language(language)
        if codec != 'linear16' or channels != 1 or not 8000 <= sample_rate <= 48000:
            raise ValueError('speech_invalid_audio_format')
        if keywords:
            raise ValueError('speech_keywords_unsupported')
        if await run_blocking(db_executor, chat.is_trial_paywalled, uid, x_app_platform):
            await websocket.close(1008, 'trial_expired')
            return
        limit, window = chat.get_effective_limit('voice:transcribe_stream')
        allowed, _, _ = await run_blocking(
            critical_executor, chat.check_rate_limit, uid, 'voice:transcribe_stream', limit, window
        )
        if not allowed:
            await websocket.close(1008, 'speech_rate_limited')
            return
    except (ValueError, TranscriptionFailure):
        await websocket.close(1008, 'speech_invalid_input')
        return
    except Exception:
        await websocket.close(1013, 'speech_admission_unavailable')
        return

    budget_reserved_ms = 0
    remaining_ms = speech.MAX_AUDIO_SECONDS * 1000
    # Reserve at admission rather than probing the shared budget and recording
    # after the socket closes. Parallel local PTT sessions must not each spend
    # the same remaining duration. A failed reservation remains fail-open like
    # the upstream duration limiter, while the local 60-second cap still holds.
    try:
        allowed, reserved_ms, _used_ms, _remaining_ms = await run_blocking(
            critical_executor, chat.try_reserve_session_budget, uid, speech.MAX_AUDIO_SECONDS * 1000
        )
        if not allowed:
            await websocket.close(1008, 'speech_rate_limited')
            return
        if reserved_ms > 0:
            budget_reserved_ms = reserved_ms
            remaining_ms = min(remaining_ms, reserved_ms)
    except Exception:
        record_fallback(
            component='ptt_cascade',
            from_mode='atomic_budget_reservation',
            to_mode='duration_settlement',
            reason='other',
            outcome='degraded',
        )

    segments = asyncio.Queue(maxsize=16)
    try:
        socket = SenseVoiceSocket(sample_rate=sample_rate, transcript_callback=segments.put_nowait)
        socket.start()
    except Exception:
        await websocket.close(1013, 'speech_provider_unavailable')
        return
    received = 0
    send_failed = False
    settlement = None

    async def drain_and_settle():
        # Accepted audio belongs to the provider even after the client leaves.
        # A failed drain or rejected provider send refunds the reservation.
        drained = False
        try:
            await socket.drain_and_close()
            drained = True
        finally:
            actual_ms = (
                chat.compute_pcm_duration_ms(received, sample_rate, channels)
                if drained and received > 0 and not send_failed
                else 0
            )
            if budget_reserved_ms > 0:
                await run_blocking(critical_executor, chat.settle_reserved_duration, uid, budget_reserved_ms, actual_ms)
            elif actual_ms > 0:
                await run_blocking(critical_executor, chat.record_actual_duration, uid, actual_ms)

    def settle_once():
        nonlocal settlement
        if settlement is None:
            settlement = asyncio.create_task(drain_and_settle())
        return settlement

    async def send_segments():
        while True:
            value = await segments.get()
            try:
                await websocket.send_json(value)
            finally:
                segments.task_done()

    sender = asyncio.create_task(send_segments())
    last_audio_at = monotonic()
    try:
        while True:
            remaining_idle = 30 - (monotonic() - last_audio_at)
            if remaining_idle <= 0:
                raise asyncio.TimeoutError
            message = await asyncio.wait_for(websocket.receive(), timeout=remaining_idle)
            if message['type'] == 'websocket.disconnect':
                return
            if message.get('text') == 'finalize':
                await asyncio.shield(settle_once())
                await asyncio.wait_for(segments.join(), timeout=5)
                if sender.done():
                    sender.result()
                await websocket.close(1000, 'speech_finalized')
                return
            data = message.get('bytes')
            if data is None:
                continue
            prospective = received + len(data)
            duration = chat.compute_pcm_duration_ms(prospective, sample_rate, channels)
            if duration > min(speech.MAX_AUDIO_SECONDS * 1000, remaining_ms) or len(data) % 2:
                await websocket.close(1008, 'speech_audio_limit')
                return
            if sender.done():
                sender.result()
            try:
                accepted = socket.send(data)
            except Exception:
                send_failed = True
                raise
            if not accepted:
                send_failed = True
                raise SpeechError('speech_provider_rejected_audio', retryable=True)
            received = prospective
            if data:
                last_audio_at = monotonic()
    except asyncio.TimeoutError:
        await websocket.close(1008, 'speech_audio_idle_timeout')
    except WebSocketDisconnect:
        pass
    except Exception:
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.send_json(
                {'type': 'service_status', 'status': 'stt_failed', 'code': 'speech_inference_failed', 'retryable': True}
            )
            await websocket.close(1011, 'speech_inference_failed')
    finally:
        # One task owns terminal drain and usage for every exit, including
        # cancellation. Shield it until complete before tearing down its sender.
        task = settle_once()
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
            except Exception:
                break
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, RuntimeError, WebSocketDisconnect):
            await sender
        if cancelled:
            raise asyncio.CancelledError


def install(app):
    # Replacing only the endpoint call preserves the actual upstream dependency
    # tree (JWT, account admission, route limits) and request decoding.
    expected = {
        ('routers.tts', 'tts_synthesize'): tts_mobile,
        ('routers.desktop_tts_updates', 'tts_synthesize'): tts,
        ('routers.chat', 'transcribe_voice_message'): prerecorded,
        ('routers.chat', 'transcribe_voice_message_stream'): ptt,
    }
    found = set()
    for route in app.routes:
        endpoint = getattr(route, 'endpoint', None)
        owner = (getattr(endpoint, '__module__', ''), getattr(endpoint, '__name__', ''))
        if owner in expected:
            route.dependant.call = expected[owner]
            found.add(owner)
    if found != set(expected):
        raise RuntimeError('local speech route owner drift: ' + repr(set(expected) - found))
