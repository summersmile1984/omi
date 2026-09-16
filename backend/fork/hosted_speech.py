"""Hosted OpenAI-compatible speech transport; bounded IO and the existing STT wire contract."""

import base64
import io
import json
import subprocess
import wave

import httpx

from . import operator_ai
from .egress_policy import assert_http_endpoint_allowed
from .speech import MAX_AUDIO_SECONDS, MAX_TEXT_CHARACTERS, SpeechError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_RESULT_BYTES = 4_000_000
MAX_WAV_BYTES = 4_000_000


def _headers():
    return {'Authorization': 'Bearer ' + operator_ai.credentials(), **operator_ai.gateway_headers()}


def _stream_json(method, endpoint, *, headers, timeout, transport=None, **kwargs):
    try:
        with httpx.Client(transport=transport, follow_redirects=False, timeout=timeout) as client:
            with client.stream(method, endpoint, headers=headers, **kwargs) as response:
                if response.status_code != 200:
                    raise SpeechError(
                        'speech_provider_http_' + str(response.status_code),
                        retryable=response.status_code == 429 or response.status_code >= 500,
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESULT_BYTES:
                        raise SpeechError('speech_provider_response_limit', retryable=True)
                result = json.loads(body)
    except SpeechError:
        raise
    except Exception:
        # Provider bodies can echo audio, text or credentials. Only typed codes
        # cross into the upstream route's logging/error boundary.
        raise SpeechError('speech_provider_request_failed', retryable=True) from None
    if not isinstance(result, dict):
        raise SpeechError('speech_provider_incomplete_result', retryable=True)
    return result


class Client:
    def transcribe_audio(
        self,
        audio_bytes,
        *,
        audio_format='wav',
        filename=None,
        content_type=None,
        language=None,
        transport=None,
        **kwargs,
    ):
        selected = operator_ai.current()
        from utils.mimo_pipeline.mimo_client import MimoSegment, MimoTranscription, infer_audio_format

        if not audio_bytes or len(audio_bytes) > MAX_UPLOAD_BYTES:
            raise SpeechError('speech_invalid_input')
        from .speech import prerecorded_selection

        language = prerecorded_selection(language)[1]
        if selected.provider == operator_ai.CLOUDFLARE_GATEWAY:
            # The direct account REST endpoint keeps the audio model's binary
            # and transcription bodies (the unified /ai/run envelope returns
            # an empty {result:{}} wrapper for TTS), still routed through the
            # operator's gateway with the cf-aig-gateway-id header.
            endpoint = selected.asr_base_url + '/run/' + selected.asr_model
            assert_http_endpoint_allowed(endpoint)
            result = _stream_json(
                'POST',
                endpoint,
                headers=_headers(),
                timeout=selected.request_timeout_seconds,
                transport=transport,
                json={'audio': base64.b64encode(audio_bytes).decode('ascii')},
            )
            payload = result.get('result') if isinstance(result.get('result'), dict) else result
            text = payload.get('text') if isinstance(payload, dict) else None
            if not isinstance(text, str):
                raise SpeechError('speech_invalid_transcript', retryable=True)
            return MimoTranscription(
                text=text, duration=0.0, segments=[MimoSegment(0, 0.0, text, 'SPEAKER_00')] if text else []
            )
        endpoint = selected.asr_base_url + '/audio/transcriptions'
        assert_http_endpoint_allowed(endpoint)
        if filename or content_type:
            audio_format = infer_audio_format(filename or '', content_type)
        if audio_format not in ('wav', 'mp3'):
            raise SpeechError('speech_invalid_audio_format')
        mime = 'audio/wav' if audio_format == 'wav' else 'audio/mpeg'
        result = _stream_json(
            'POST',
            endpoint,
            headers=_headers(),
            timeout=selected.request_timeout_seconds,
            transport=transport,
            files={'file': ('audio.' + audio_format, audio_bytes, mime)},
            data={'model': selected.asr_model},
        )
        text = result.get('text')
        if not isinstance(text, str):
            raise SpeechError('speech_invalid_transcript', retryable=True)
        duration = 0.0
        usage = result.get('usage')
        if isinstance(usage, dict):
            try:
                duration = float(usage.get('seconds', 0))
            except (TypeError, ValueError):
                duration = 0.0
        return MimoTranscription(
            text=text, duration=duration, segments=[MimoSegment(0, duration, text, 'SPEAKER_00')] if text else []
        )


def synthesize(text, voice=None, *, transport=None):
    selected = operator_ai.current()
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARACTERS:
        raise SpeechError('speech_invalid_input')
    if selected.provider == operator_ai.CLOUDFLARE_GATEWAY:
        endpoint = selected.tts_base_url + '/run/' + selected.tts_model
        assert_http_endpoint_allowed(endpoint)
        try:
            with httpx.Client(
                transport=transport, follow_redirects=False, timeout=selected.request_timeout_seconds
            ) as client:
                with client.stream(
                    'POST',
                    endpoint,
                    headers=_headers(),
                    json={'text': text},
                ) as response:
                    if response.status_code != 200:
                        raise SpeechError(
                            'speech_provider_http_' + str(response.status_code),
                            retryable=response.status_code == 429 or response.status_code >= 500,
                        )
                    audio = bytearray()
                    for chunk in response.iter_bytes():
                        audio.extend(chunk)
                        if len(audio) > MAX_WAV_BYTES:
                            raise SpeechError('speech_provider_response_limit', retryable=True)
        except SpeechError:
            raise
        except Exception:
            raise SpeechError('speech_provider_request_failed', retryable=True) from None
        return _as_mono_wav(bytes(audio))
    endpoint = selected.tts_base_url + '/audio/speech'
    assert_http_endpoint_allowed(endpoint)
    payload = {
        'model': selected.tts_model,
        'input': text,
        'voice': voice or selected.tts_voice,
        'response_format': selected.tts_response_format,
    }
    try:
        with httpx.Client(
            transport=transport, follow_redirects=False, timeout=selected.request_timeout_seconds
        ) as client:
            with client.stream('POST', endpoint, headers=_headers(), json=payload) as response:
                if response.status_code != 200:
                    raise SpeechError(
                        'speech_provider_http_' + str(response.status_code),
                        retryable=response.status_code == 429 or response.status_code >= 500,
                    )
                audio = bytearray()
                for chunk in response.iter_bytes():
                    audio.extend(chunk)
                    if len(audio) > MAX_WAV_BYTES:
                        raise SpeechError('speech_provider_response_limit', retryable=True)
    except SpeechError:
        raise
    except Exception:
        raise SpeechError('speech_provider_request_failed', retryable=True) from None
    return _as_mono_wav(bytes(audio))


def _as_mono_wav(audio):
    """Admit a decoded 16-bit mono WAV envelope, or normalize any decodable audio to one."""
    try:
        with wave.open(io.BytesIO(audio)) as decoded:
            if (
                decoded.getnchannels() == 1
                and decoded.getsampwidth() == 2
                and 0 < decoded.getnframes() <= decoded.getframerate() * MAX_AUDIO_SECONDS
            ):
                return audio
    except Exception:
        pass
    try:
        result = subprocess.run(
            ['ffmpeg', '-v', 'error', '-nostdin', '-i', 'pipe:0', '-f', 'wav', '-ac', '1', 'pipe:1'],
            input=audio,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise SpeechError('speech_invalid_audio_result', retryable=True) from None
    if not result.stdout or len(result.stdout) > MAX_WAV_BYTES:
        raise SpeechError('speech_invalid_audio_result', retryable=True)
    # A piped ffmpeg WAV header carries a placeholder frame count; rewrite the
    # envelope with the real frame count so downstream consumers keep the
    # bounded-wire contract the MiMo owner returned.
    with wave.open(io.BytesIO(result.stdout)) as decoded:
        channels, sampwidth, rate = decoded.getnchannels(), decoded.getsampwidth(), decoded.getframerate()
        frames = decoded.readframes(decoded.getnframes())
    if channels != 1 or sampwidth != 2:
        raise SpeechError('speech_invalid_audio_result', retryable=True)
    if not 0 < len(frames) // 2 <= rate * MAX_AUDIO_SECONDS:
        raise SpeechError('speech_invalid_audio_result', retryable=True)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(frames)
    return buffer.getvalue()


def prerecorded():
    from utils.mimo_pipeline.prerecorded_provider import MimoPrerecordedProvider

    class Provider(MimoPrerecordedProvider):
        def transcribe_url(
            self,
            audio_url,
            speakers_count=None,
            attempts=0,
            return_language=False,
            diarize=True,
            language=None,
            keywords=None,
        ):
            assert_http_endpoint_allowed(audio_url)
            with httpx.stream('GET', audio_url, timeout=120, follow_redirects=False) as response:
                response.raise_for_status()
                audio = bytearray()
                for chunk in response.iter_bytes():
                    audio.extend(chunk)
                    if len(audio) > MAX_UPLOAD_BYTES:
                        raise SpeechError('speech_invalid_input')
                result = self._client.transcribe_audio(
                    bytes(audio),
                    filename=audio_url,
                    content_type=response.headers.get('content-type'),
                    language=language,
                )
            segments = self._segments(result.text, result.duration)
            return (segments, language or 'multi') if return_language else segments

    return Provider(client=Client())
