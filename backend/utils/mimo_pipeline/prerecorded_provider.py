"""Pre-recorded STT adapter for the MiMo ASR API."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import httpx

from utils.mimo_pipeline.mimo_client import MAX_AUDIO_BYTES, MimoClient, infer_audio_format
from utils.mimo_pipeline.socket import pcm16_to_wav
from utils.stt.pre_recorded import PrerecordedSTTProvider

_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


def _raw_pcm_encoding(encoding: Optional[str]) -> bool:
    return (encoding or '').strip().lower() in {'linear16', 'pcm', 'pcm16', 's16le'}


class MimoPrerecordedProvider(PrerecordedSTTProvider):
    """Transcribe complete recordings without pretending the API is streaming."""

    def __init__(self, client: Optional[MimoClient] = None) -> None:
        self._client = client or MimoClient()

    @staticmethod
    def _segments(text: str, duration: float) -> List[Dict[str, Any]]:
        normalized = text.strip()
        if not normalized:
            return []
        return [{'timestamp': [0.0, duration], 'speaker': 'SPEAKER_00', 'text': normalized}]

    def _transcribe_bytes(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int,
        channels: int,
        encoding: Optional[str],
        language: Optional[str],
    ) -> Tuple[List[Dict[str, Any]], str]:
        if _raw_pcm_encoding(encoding):
            prepared = pcm16_to_wav(audio_bytes, sample_rate, channels)
            audio_format = 'wav'
            duration = len(audio_bytes) / max(1, 2 * channels * sample_rate)
        else:
            prepared = audio_bytes
            audio_format = infer_audio_format('', encoding)
            duration = 0.0
        result = self._client.transcribe_audio(prepared, audio_format=audio_format, language=language)
        return self._segments(result.text, duration or result.duration), language or 'multi'

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
        response = httpx.get(audio_url, timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        if len(response.content) > MAX_AUDIO_BYTES:
            raise ValueError(f'pre-recorded audio exceeds MiMo limit ({len(response.content)} bytes)')
        result = self._client.transcribe_audio(
            response.content,
            filename=audio_url,
            content_type=response.headers.get('content-type'),
            language=language,
        )
        words = self._segments(result.text, result.duration)
        return (words, language or 'multi') if return_language else words

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
        words, detected_language = self._transcribe_bytes(
            audio_bytes,
            sample_rate=sample_rate,
            channels=channels,
            encoding=encoding,
            language=language,
        )
        return (words, detected_language) if return_language else words
