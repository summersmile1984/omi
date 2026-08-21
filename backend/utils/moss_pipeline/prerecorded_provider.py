"""MOSS provider implementing the upstream PrerecordedSTTProvider contract.

Drop-in replacement for Parakeet/Modulate pre-recorded STT: uses an explicitly
configured MOSS-compatible operator endpoint for transcription + diarization.
Speaker *identification* against known people is a separate step that runs
locally on CPU (see pipeline.MossSpeakerPipeline / identify step).

All MOSS-specific code lives under utils/moss_pipeline/; the only upstream
touch-points are:
  - config/prerecorded_stt.py  : one new service value ('moss')
  - utils/stt/pre_recorded.py  : one branch in get_prerecorded_provider()
  - utils/stt/pre_recorded.py  : 'moss' token accepted in get_prerecorded_models()
"""

from __future__ import annotations

import logging
import tempfile
import wave
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from utils.stt.pre_recorded import PrerecordedSTTProvider

logger = logging.getLogger(__name__)


class MossPrerecordedProvider(PrerecordedSTTProvider):
    """Transcribe + diarize via the MOSS API, shaped like the upstream output.

    Returned segments: ``[{'timestamp': [start, end], 'speaker': 'SPEAKER_XX',
    'text': ...}]`` — identical to Parakeet/Modulate providers, so every
    downstream consumer (sync pipeline, conversation persistence) works
    unchanged.
    """

    def __init__(self) -> None:
        from .moss_client import create_moss_client

        self._client = create_moss_client()

    def _transcribe(
        self,
        audio_source: str,
        *,
        is_url: bool,
        diarize: bool,
        language: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        from .moss_client import MlxAudioClient, download_audio_url

        if isinstance(self._client, MlxAudioClient):
            if is_url:
                audio_bytes = download_audio_url(audio_source)
                filename = 'audio.wav'
            else:
                with open(audio_source, 'rb') as source_file:
                    audio_bytes = source_file.read()
                filename = os.path.basename(audio_source) or 'audio.wav'
            transcription = self._client.transcribe_audio(
                audio_bytes,
                filename=filename,
                language=language,
                diarize=diarize,
            )
        elif is_url:
            # The MOSS transport validates this URL before forwarding it to the
            # operator endpoint; it is never accepted as an unguarded egress.
            transcription = self._client.transcribe(url=audio_source, model="moss-transcribe-diarize", diarize=diarize)
        else:
            file_id = self._client.upload_file(audio_source)
            try:
                transcription = self._client.transcribe(
                    file_id=file_id, model="moss-transcribe-diarize", diarize=diarize
                )
            finally:
                self._client.delete_file(file_id)

        return self._words_from_transcription(transcription)

    def transcribe_url(
        self,
        audio_url: str,
        speakers_count: Optional[int] = None,
        attempts: int = 0,
        return_language: bool = False,
        diarize: bool = True,
        language: Optional[str] = None,
        keywords: Optional[Sequence[str]] = None,
    ) -> Union[List[Dict[str, Any]], Tuple[List[Dict[str, Any]], str]]:
        words = self._transcribe(audio_url, is_url=True, diarize=diarize, language=language)
        if return_language:
            return words, language or "en"
        return words

    def transcribe_bytes(
        self,
        audio_bytes: bytes,
        sample_rate: int = 16000,
        diarize: bool = True,
        attempts: int = 0,
        encoding: Optional[str] = None,
        channels: int = 1,
        language: Optional[str] = None,
        return_language: bool = False,
        keywords: Optional[Sequence[str]] = None,
    ) -> Union[List[Dict[str, Any]], Tuple[List[Dict[str, Any]], str]]:
        from .moss_client import MlxAudioClient

        if isinstance(self._client, MlxAudioClient):
            transcription = self._client.transcribe_audio(
                audio_bytes,
                filename='audio.wav',
                language=language,
                diarize=diarize,
            )
            words = self._words_from_transcription(transcription)
        else:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
                tmp.write(audio_bytes)
                tmp.flush()
                words = self._transcribe(tmp.name, is_url=False, diarize=diarize, language=language)
        if return_language:
            return words, language or "en"
        return words

    @staticmethod
    def _words_from_transcription(transcription: Any) -> List[Dict[str, Any]]:
        words: List[Dict[str, Any]] = []
        for seg in transcription.segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            speaker = seg.speaker or "S01"
            if not speaker.startswith("SPEAKER_"):
                speaker = f"SPEAKER_{speaker.lstrip('S0') or '00'}"
            words.append({"timestamp": [seg.start, seg.end], "speaker": speaker, "text": text})
        if not words and transcription.text:
            words.append({"timestamp": [0.0, 0.0], "speaker": "SPEAKER_00", "text": transcription.text})
        return words


def moss_prerecorded_requested() -> bool:
    return os.getenv("STT_PRERECORDED_MODEL", "").strip().lower().split(",", 1)[0] == "moss"


def require_moss_prerecorded() -> None:
    from .config import resolve_moss_config

    resolve_moss_config()


def moss_prerecorded_enabled() -> bool:
    """True only when explicit MOSS selection and a safe runtime config exist."""

    if not moss_prerecorded_requested():
        return False
    from .config import moss_is_configured

    return moss_is_configured()
