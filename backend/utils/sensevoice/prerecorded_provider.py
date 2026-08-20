"""Local pre-recorded STT adapter for SenseVoice."""

from __future__ import annotations

from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import httpx
from pydub import AudioSegment

from utils.sensevoice.socket import get_sensevoice_recognizer, pcm16_to_samples
from utils.stt.pre_recorded import PrerecordedSTTProvider

_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


def _as_pcm16_mono(
    audio_bytes: bytes, *, sample_rate: int, channels: int, encoding: Optional[str]
) -> Tuple[bytes, int]:
    normalized = (encoding or '').strip().lower()
    if normalized in {'linear16', 'pcm', 'pcm16', 's16le'}:
        if channels == 1:
            return audio_bytes, sample_rate
        segment = AudioSegment(data=audio_bytes, sample_width=2, frame_rate=sample_rate, channels=channels)
    else:
        format_hint = normalized.split('/', 1)[-1] if normalized else None
        if format_hint in {'mpeg', 'mp3'}:
            format_hint = 'mp3'
        segment = AudioSegment.from_file(BytesIO(audio_bytes), format=format_hint)
    prepared = segment.set_channels(1).set_frame_rate(16000).set_sample_width(2)
    raw_data = prepared.raw_data
    frame_rate = prepared.frame_rate
    if not isinstance(raw_data, bytes):
        raise TypeError('decoded audio did not produce an immutable PCM byte buffer')
    if not isinstance(frame_rate, int) or frame_rate <= 0:
        raise ValueError('decoded audio did not produce a valid sample rate')
    return raw_data, frame_rate


class SenseVoicePrerecordedProvider(PrerecordedSTTProvider):
    """Run the offline SenseVoice recognizer on complete recordings."""

    def __init__(self, recognizer: Any = None) -> None:
        self._recognizer = recognizer

    def _transcribe_bytes(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int,
        channels: int,
        encoding: Optional[str],
        language: Optional[str],
    ) -> Tuple[List[Dict[str, Any]], str]:
        pcm, prepared_rate = _as_pcm16_mono(
            audio_bytes,
            sample_rate=sample_rate,
            channels=channels,
            encoding=encoding,
        )
        recognizer = self._recognizer or get_sensevoice_recognizer()
        stream = recognizer.create_stream()
        stream.accept_waveform(prepared_rate, pcm16_to_samples(pcm))
        recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        duration = len(pcm) / max(1, 2 * prepared_rate)
        words = [] if not text else [{'timestamp': [0.0, duration], 'speaker': 'SPEAKER_00', 'text': text}]
        return words, language or 'multi'

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
        words, detected_language = self._transcribe_bytes(
            response.content,
            sample_rate=16000,
            channels=1,
            encoding=response.headers.get('content-type'),
            language=language,
        )
        return (words, detected_language) if return_language else words

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
