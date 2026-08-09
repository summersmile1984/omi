"""Local SenseVoice-Small streaming socket (CPU, no API, no GPU).

Implements the upstream ``STTSocket`` contract so the live listen path can
use SenseVoice-Small entirely on the local machine:
  - ``send()`` accumulates mono PCM16 frames
  - ``finish()`` runs SenseVoice (sherpa-onnx, CPU) over the accumulated
    audio and emits the transcript via the provided callback
  - ``finalize()`` / ``is_connection_dead`` / ``death_reason`` satisfy the
    socket contract

Why not MOSS for live: MOSS is batch/offline (even its SSE returns streamed
*text* for a whole uploaded file). Live audio needs incremental local
inference — SenseVoice-Small on CPU is the documented path (see
omi-subscription-margin.md: 234M, CPU 17.2x realtime, Chinese CER 7.81%).

All code lives in utils/sensevoice/; the only upstream touch is the socket
selection branch in the streaming provider.
"""

from __future__ import annotations

import array
import logging
import os
import threading
from typing import Any, Callable, List, Optional

from utils.stt.socket import STTSocket

logger = logging.getLogger(__name__)

# Env config
SENSEVOICE_MODEL_DIR = os.getenv(
    "SENSEVOICE_MODEL_DIR",
    "/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
)
SENSEVOICE_THREADS = int(os.getenv("SENSEVOICE_NUM_THREADS", "4"))
SENSEVOICE_USE_ITN = os.getenv("SENSEVOICE_USE_ITN", "1") == "1"


# ---------------------------------------------------------------------------
# Lazy singleton recognizer (CPU)
# ---------------------------------------------------------------------------

_recognizer: Any = None
_recognizer_lock = threading.Lock()


def get_sensevoice_recognizer() -> Any:
    """Return the process-wide sherpa-onnx SenseVoice recognizer (CPU)."""
    global _recognizer
    if _recognizer is None:
        with _recognizer_lock:
            if _recognizer is None:
                import sherpa_onnx

                model = os.path.join(SENSEVOICE_MODEL_DIR, "model.int8.onnx")
                tokens = os.path.join(SENSEVOICE_MODEL_DIR, "tokens.txt")
                if not (os.path.exists(model) and os.path.exists(tokens)):
                    raise RuntimeError(
                        f"SenseVoice model not found at {SENSEVOICE_MODEL_DIR} "
                        "(set SENSEVOICE_MODEL_DIR)"
                    )
                _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=model,
                    tokens=tokens,
                    num_threads=SENSEVOICE_THREADS,
                    use_itn=SENSEVOICE_USE_ITN,
                )
                logger.info("SenseVoice recognizer loaded (CPU, threads=%d)", SENSEVOICE_THREADS)
    return _recognizer


def _pcm16_to_samples(pcm: bytes) -> List[int]:
    return list(array.array("h", pcm))


# ---------------------------------------------------------------------------
# Socket
# ---------------------------------------------------------------------------


class SenseVoiceSocket(STTSocket):
    """Accumulate PCM16 audio; transcribe with SenseVoice on finish().

    ``transcript_callback`` is called with the final transcript text (and the
    audio duration in seconds) when the stream ends.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        transcript_callback: Optional[Callable[[str, float], None]] = None,
        recognizer: Any = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._callback = transcript_callback
        self._recognizer = recognizer
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
        """End of audio: run SenseVoice over the accumulated PCM."""
        if self._finished:
            return
        self._finished = True
        try:
            recognizer = self._recognizer or get_sensevoice_recognizer()
            stream = recognizer.create_stream()
            stream.accept_waveform(self._sample_rate, _pcm16_to_samples(bytes(self._pcm)))
            recognizer.decode_stream(stream)
            text = stream.result.text.strip()
            duration = len(self._pcm) / (2 * self._sample_rate)
            if self._callback:
                self._callback(text, duration)
            logger.info("SenseVoice transcript: %r (%.1fs)", text, duration)
        except Exception as exc:  # pragma: no cover - local inference failure
            logger.error("SenseVoice finish failed: %s", exc)
            self._dead = True
            self._death_reason = f"sensevoice_decode:{type(exc).__name__}"

    def finalize(self) -> None:
        self._pcm.clear()

    @property
    def is_connection_dead(self) -> bool:
        return self._dead

    @property
    def death_reason(self) -> Optional[str]:
        return self._death_reason
