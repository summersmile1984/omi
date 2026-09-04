"""Admitted local CPU speech models. No network, provider fallback or downloads."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import io
import os
from pathlib import Path
import threading
import wave

from . import profile
from .model_contract import validate_speech
from .speech_assets import verify

STT_LANGUAGES = frozenset({'en', 'zh', 'ja', 'ko', 'yue', 'multi'})
TTS_VOICES = {'af_heart': 3, 'zf_xiaobei': 45}
MAX_AUDIO_SECONDS = 60
MAX_TEXT_CHARACTERS = 500


class SpeechError(RuntimeError):
    def __init__(self, code, *, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


def contract():
    row = profile.current()
    if row.get('target') != 'self_hosted':
        raise SpeechError('speech_profile_required')
    result = validate_speech(row.get('speech'))
    if result is None:
        from .capabilities import Capability, reject

        reject(Capability.STT)
    return result


def language(value):
    normalized = (value or 'multi').strip().lower().replace('_', '-').split('-')[0]
    normalized = 'multi' if normalized == 'auto' else normalized
    if normalized not in STT_LANGUAGES:
        from config.prerecorded_stt import TranscriptionOutcome
        from utils.stt.outcomes import TranscriptionFailure

        raise TranscriptionFailure(TranscriptionOutcome.INVALID_INPUT, provider='sensevoice', retryable=False)
    return normalized


def prerecorded_selection(value='en'):
    return 'sensevoice', language(value), contract().stt_model


def streaming_selection(
    value='en', multi_lang_enabled=True, *, surface=None, preferred_service=None, exclude=frozenset()
):
    from utils.stt.streaming import STTService

    selected = contract()
    if 'sensevoice' in exclude:
        return None, None, None
    return STTService.sensevoice, language(value), selected.stt_model


class Runtime:
    def __init__(self, selected, root):
        import sherpa_onnx

        if sherpa_onnx.__version__ != selected.runtime_version:
            raise SpeechError('speech_runtime_version_mismatch')
        verify(root, selected)
        from utils.stt import vad

        with open(vad._MODEL_PATH, 'rb') as stream:
            digest = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != selected.vad_digest:
            raise SpeechError('speech_vad_artifact_mismatch')
        vad._get_ort_session()
        stt, tts = root / selected.stt_model, root / selected.tts_model
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(stt / 'model.int8.onnx'),
            tokens=str(stt / 'tokens.txt'),
            num_threads=2,
            provider='cpu',
            language='auto',
            use_itn=True,
        )
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(tts / 'model.onnx'),
                    voices=str(tts / 'voices.bin'),
                    tokens=str(tts / 'tokens.txt'),
                    data_dir=str(tts / 'espeak-ng-data'),
                    lexicon=str(tts / 'lexicon-us-en.txt') + ',' + str(tts / 'lexicon-zh.txt'),
                ),
                num_threads=2,
                provider='cpu',
            ),
            max_num_sentences=1,
        )
        if not config.validate():
            raise SpeechError('speech_tts_configuration_invalid')
        self.tts = sherpa_onnx.OfflineTts(config)
        self.tts_gate = threading.Lock()

    def synthesize(self, text, voice):
        import numpy as np

        if not text.strip() or len(text) > MAX_TEXT_CHARACTERS or voice not in TTS_VOICES:
            raise SpeechError('speech_invalid_input')
        if not self.tts_gate.acquire(blocking=False):
            raise SpeechError('speech_busy', retryable=True)
        try:
            result = self.tts.generate(text, sid=TTS_VOICES[voice], speed=1.0)
            samples = np.asarray(result.samples, dtype=np.float32)
            if (
                result.sample_rate != 24000
                or samples.ndim != 1
                or not samples.size
                or samples.size > 24000 * MAX_AUDIO_SECONDS
                or not np.isfinite(samples).all()
                or not np.any(samples)
            ):
                raise SpeechError('speech_invalid_audio_result', retryable=True)
            buffer = io.BytesIO()
            with wave.open(buffer, 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(result.sample_rate)
                output.writeframes(np.clip(samples * 32768, -32768, 32767).astype('<i2').tobytes())
            return buffer.getvalue()
        except SpeechError:
            raise
        except Exception as error:
            raise SpeechError('speech_inference_failed', retryable=True) from error
        finally:
            self.tts_gate.release()


@lru_cache(maxsize=1)
def runtime():
    selected = contract()
    value = os.environ.get('SPEECH_MODEL_STORE', '').strip()
    if not value or not Path(value).is_absolute():
        raise SpeechError('speech_model_store_required')
    return Runtime(selected, Path(value))


def recognizer():
    return runtime().recognizer


def check():
    """Fail boot unless real synthesis and recognition both execute successfully."""
    from utils.sensevoice.socket import decode_pcm

    model = runtime()
    audio = model.synthesize('The local speech service is ready.', 'af_heart')
    with wave.open(io.BytesIO(audio)) as stream:
        text = decode_pcm(model.recognizer, stream.getframerate(), stream.readframes(stream.getnframes()))
    if not text:
        raise SpeechError('speech_readiness_empty_transcript')


if __name__ == '__main__':
    check()
    print('Local speech artifact and inference readiness passed.')
