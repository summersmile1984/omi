"""MOSS provider implementing the upstream PrerecordedSTTProvider contract.

Drop-in replacement for Parakeet/Modulate pre-recorded STT: uses the MOSS
official API (api.mosi.cn) for transcription + diarization (no GPU needed).
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
        from .moss_client import MossClient

        self._client = MossClient()

    def _transcribe(self, audio_source: str, *, is_url: bool, diarize: bool) -> List[Dict[str, Any]]:
        # MOSS takes a public URL or an uploaded file id; there is no inline
        # base64. For bytes we upload a temp file; for URLs we pass through.
        if is_url:
            transcription = self._client.transcribe(url=audio_source, model="moss-transcribe-diarize", diarize=diarize)
        else:
            file_id = self._client.upload_file(audio_source)
            try:
                transcription = self._client.transcribe(
                    file_id=file_id, model="moss-transcribe-diarize", diarize=diarize
                )
            finally:
                self._client.delete_file(file_id)

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
        words = self._transcribe(audio_url, is_url=True, diarize=diarize)
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
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
            tmp.write(audio_bytes)
            tmp.flush()
            words = self._transcribe(tmp.name, is_url=False, diarize=diarize)
        if return_language:
            return words, language or "en"
        return words
