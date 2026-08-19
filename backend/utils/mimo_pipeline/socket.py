"""MiMo-V2.5-ASR live (streaming) STT socket.

Implements the upstream ``STTSocket`` contract so the live listen path can
select MiMo-V2.5-ASR as the streaming STT provider (``STT_SERVICE_MODELS``
contains ``mimo`` and ``MIMO_API_KEY`` is set).

MiMo's ASR API is a chat-completions endpoint (not a WebSocket): it accepts
base64 audio and returns the transcript. The socket therefore accumulates
PCM16 audio during the session and calls the API once on ``finish()`` —
the same accumulate-then-transcribe pattern as ``SenseVoiceSocket``, with the
audio transcoding to MiMo's server side instead of a local sherpa-onnx model.
"""

from __future__ import annotations

import logging
import os
import tempfile
import wave
from typing import Any, Callable, Optional

from utils.mimo_pipeline.mimo_client import MimoAPIError, MimoClient
from utils.stt.socket import STTSocket

logger = logging.getLogger(__name__)


def mimo_available() -> bool:
    """True when MiMo streaming STT is configured (key set)."""
    return bool(os.getenv("MIMO_API_KEY"))


def _pcm16_to_wav(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    """Wrap raw PCM16 little-endian audio in a WAV container (MiMo wants wav)."""
    with tempfile.SpooledTemporaryFile() as tmp:
        with wave.open(tmp, "wb") as w:
            w.setnchannels(channels or 1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(pcm)
        tmp.seek(0)
        return tmp.read()


class MimoSttSocket(STTSocket):
    """Accumulate PCM16 audio; transcribe with MiMo-V2.5-ASR on finish()."""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        transcript_callback: Optional[Callable[[str, float], None]] = None,
        client: Any = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._channels = channels or 1
        self._callback = transcript_callback
        self._client = client
        self._pcm = bytearray()
        self._finished = False
        self._dead = False
        self._death_reason: Optional[str] = None

    # -- STTSocket contract -------------------------------------------------
    def send(self, data: bytes) -> bool:
        if self._finished or self._dead:
            return False
        self._pcm.extend(data)
        return True

    def finish(self) -> None:
        """End of audio: transcribe the accumulated PCM via MiMo ASR API."""
        if self._finished:
            return
        self._finished = True
        try:
            audio = _pcm16_to_wav(bytes(self._pcm), self._sample_rate, self._channels)
            client = self._client or MimoClient()
            transcription = client.transcribe_audio(audio, audio_format="wav")
            text = (transcription.text or "").strip()
            duration = len(self._pcm) / (2 * self._sample_rate)
            if self._callback:
                self._callback(text, duration)
            logger.info("MiMo STT transcript: %r (%.1fs)", text, duration)
        except MimoAPIError as exc:
            logger.error("MiMo STT finish failed: %s", exc)
            self._dead = True
            self._death_reason = f"mimo_api:{type(exc).__name__}"
        except Exception as exc:  # pragma: no cover - unexpected client failure
            logger.error("MiMo STT finish failed: %s", exc)
            self._dead = True
            self._death_reason = f"mimo_stt:{type(exc).__name__}"

    def finalize(self) -> None:
        self._pcm.clear()

    @property
    def is_connection_dead(self) -> bool:
        return self._dead

    @property
    def death_reason(self) -> Optional[str]:
        return self._death_reason
