"""Selected MiMo speech transport; bounded IO and the existing STT wire contract."""

import base64
import io
import json
from pathlib import Path
import subprocess
import tempfile
import wave

import httpx

from config.prerecorded_stt import TranscriptionOutcome
from utils.mimo_pipeline.mimo_client import MAX_AUDIO_BYTES, MimoSegment, MimoTranscription
from utils.mimo_pipeline.socket import pcm16_to_wav
from utils.stt.outcomes import TranscriptionFailure

from . import operator_ai
from .egress_policy import assert_http_endpoint_allowed
from .speech import SpeechError


def _prepare_audio(audio):
    """Identify real containers; never trust a filename or a default format hint."""
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise TranscriptionFailure(TranscriptionOutcome.INVALID_INPUT, provider='mimo', retryable=False)
    # Live PCM windows already have a canonical WAV envelope. Validate it without
    # starting a decoder for every window, and leave its rate/channels unchanged.
    try:
        with wave.open(io.BytesIO(audio)) as decoded:
            frames = decoded.getnframes()
            if frames > 0 and len(decoded.readframes(frames)) == (
                frames * decoded.getnchannels() * decoded.getsampwidth()
            ):
                return audio, 'audio/wav'
    except (wave.Error, EOFError):
        pass

    # Reuse the installed FFmpeg boundary, with seekable private input for M4A
    # files whose metadata follows the samples. No playlists or nested network
    # protocols; both subprocess and temporary-file lifetimes are bounded.
    formats = 'wav,mp3,mov,ogg,flac,matroska,webm,aac,aiff'
    try:
        with tempfile.TemporaryDirectory(prefix='mimo-audio-') as directory:
            source = Path(directory) / 'input'
            source.write_bytes(audio)
            input_args = ['-protocol_whitelist', 'file', '-format_whitelist', formats, '-i', str(source)]
            probe = subprocess.run(
                ['ffprobe', '-v', 'error', *input_args, '-show_entries', 'format=format_name', '-of', 'json'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=True,
            )
            is_mp3 = json.loads(probe.stdout)['format']['format_name'] == 'mp3'
            # MP3 is already a documented MiMo container. Decode for validation
            # but retain the original bytes, including long recordings that fit
            # the compressed 10 MiB provider limit rather than the WAV limit.
            output_args = (
                ['-f', 'null', '-']
                if is_mp3
                else ['-t', str(MAX_AUDIO_BYTES / 32000 + 0.1), '-f', 's16le', '-ac', '1', '-ar', '16000', 'pipe:1']
            )
            decoded = subprocess.run(
                ['ffmpeg', '-v', 'error', '-nostdin', '-xerror', *input_args, '-map', '0:a:0', *output_args],
                stdout=subprocess.DEVNULL if is_mp3 else subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=True,
            )
            if is_mp3:
                return audio, 'audio/mpeg'
            if not decoded.stdout or len(decoded.stdout) + 44 > MAX_AUDIO_BYTES:
                raise ValueError('invalid decoded audio size')
            return pcm16_to_wav(decoded.stdout, 16000, 1), 'audio/wav'
    except subprocess.TimeoutExpired:
        raise TranscriptionFailure(TranscriptionOutcome.TIMEOUT, provider='mimo') from None
    except OSError:
        raise TranscriptionFailure(TranscriptionOutcome.CONFIG_ERROR, provider='mimo', retryable=False) from None
    except (subprocess.CalledProcessError, ValueError, KeyError, TypeError):
        raise TranscriptionFailure(TranscriptionOutcome.INVALID_INPUT, provider='mimo', retryable=False) from None


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
        audio_bytes, mime = _prepare_audio(audio_bytes)
        from .speech import prerecorded_selection

        language = prerecorded_selection(language)[1]
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
    from .speech import windowed_socket

    return windowed_socket(sample_rate, transcript_callback, language, client_factory=Client)
