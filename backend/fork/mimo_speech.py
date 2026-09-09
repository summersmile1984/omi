"""Selected MiMo speech transport; bounded IO and the existing STT wire contract."""

import base64
import io
import wave

import httpx

from . import operator_ai
from .egress_policy import assert_http_endpoint_allowed
from .speech import SpeechError


def request(payload, *, transport=None):
    selected = operator_ai.current()
    endpoint = selected.base_url + '/chat/completions'
    assert_http_endpoint_allowed(endpoint)
    try:
        with httpx.Client(
            transport=transport, follow_redirects=False, timeout=selected.request_timeout_seconds
        ) as client:
            with client.stream(
                'POST', endpoint, headers={'Authorization': 'Bearer ' + operator_ai.credentials()}, json=payload
            ) as response:
                if response.status_code != 200:
                    raise SpeechError(
                        'speech_provider_http_' + str(response.status_code),
                        retryable=response.status_code == 429 or response.status_code >= 500,
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 4_000_000:
                        raise SpeechError('speech_provider_response_limit', retryable=True)
                import json

                result = json.loads(body)
        choice = result['choices'][0]
        if result.get('model') != payload['model'] or choice.get('finish_reason') != 'stop':
            raise SpeechError('speech_provider_incomplete_result', retryable=True)
        return result
    except SpeechError:
        raise
    except Exception as error:
        # Provider bodies can echo audio, text or credentials. Only typed codes
        # cross into the upstream route's logging/error boundary.
        raise SpeechError('speech_provider_request_failed', retryable=True) from None


class Client:
    def transcribe_audio(
        self, audio_bytes, *, audio_format='wav', filename=None, content_type=None, language=None, **kwargs
    ):
        from utils.mimo_pipeline.mimo_client import MimoSegment, MimoTranscription, infer_audio_format

        if not audio_bytes or len(audio_bytes) > 10 * 1024 * 1024:
            raise SpeechError('speech_invalid_input')
        if filename or content_type:
            audio_format = infer_audio_format(filename or '', content_type)
        if audio_format not in ('wav', 'mp3'):
            raise SpeechError('speech_invalid_audio_format')
        from .speech import prerecorded_selection

        language = prerecorded_selection(language)[1]
        mime = 'audio/wav' if audio_format == 'wav' else 'audio/mpeg'
        result = request(
            {
                'model': operator_ai.current().asr_model,
                'messages': [
                    {
                        'role': 'user',
                        'content': [
                            {
                                'type': 'input_audio',
                                'input_audio': {
                                    'data': 'data:' + mime + ';base64,' + base64.b64encode(audio_bytes).decode('ascii')
                                },
                            }
                        ],
                    }
                ],
                'asr_options': {'language': 'auto' if language == 'multi' else language},
                'stream': False,
            }
        )
        text = result['choices'][0]['message'].get('content')
        if not isinstance(text, str):
            raise SpeechError('speech_invalid_transcript', retryable=True)
        duration = float(result.get('usage', {}).get('seconds', 0))
        return MimoTranscription(
            text=text, duration=duration, segments=[MimoSegment(0, duration, text, 'SPEAKER_00')] if text else []
        )


def synthesize(text, voice='mimo_default'):
    result = request(
        {
            'model': operator_ai.current().tts_model,
            'messages': [{'role': 'assistant', 'content': text}],
            'audio': {'format': 'wav', 'voice': voice},
            'stream': False,
        }
    )
    try:
        audio = base64.b64decode(result['choices'][0]['message']['audio']['data'], validate=True)
        with wave.open(io.BytesIO(audio)) as decoded:
            if (
                decoded.getnchannels() != 1
                or decoded.getsampwidth() != 2
                or not 0 < decoded.getnframes() <= decoded.getframerate() * 60
            ):
                raise ValueError('invalid audio')
        return audio
    except Exception:
        raise SpeechError('speech_invalid_audio_result', retryable=True) from None


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
                    if len(audio) > 10 * 1024 * 1024:
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


def socket(sample_rate, transcript_callback, language='multi'):
    from utils.sensevoice.socket import SenseVoiceSocket
    from utils.mimo_pipeline.socket import pcm16_to_wav
    from utils.executors import run_blocking, sync_executor
    from utils.stt.vad import linear16_pcm_is_silent

    class MiMoSocket(SenseVoiceSocket):
        """Reuse bounded buffering, drain and cancellation ownership; replace inference."""

        async def _flush(self, *, force):
            while True:
                with self._lock:
                    available = len(self._pcm)
                    if available < self._window_bytes and not (force and available):
                        return
                    take = min(available, self._window_bytes)
                    pcm = bytes(self._pcm[:take])
                    del self._pcm[:take]
                    start = self._emitted_seconds
                    duration = take / (2 * self._sample_rate)
                    self._emitted_seconds += duration
                if await run_blocking(
                    sync_executor, linear16_pcm_is_silent, pcm, sample_rate=self._sample_rate, channels=1
                ):
                    continue
                result = await run_blocking(
                    sync_executor, Client().transcribe_audio, pcm16_to_wav(pcm, self._sample_rate, 1), language=language
                )
                if self._callback and result.text:
                    self._callback(
                        [
                            {
                                'speaker': 'SPEAKER_00',
                                'start': start,
                                'end': start + duration,
                                'text': result.text,
                                'is_user': False,
                                'person_id': None,
                            }
                        ]
                    )

    result = MiMoSocket(sample_rate=sample_rate, transcript_callback=transcript_callback)
    result.start()
    return result
