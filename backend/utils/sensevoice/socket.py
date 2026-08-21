"""Incremental local SenseVoice STT for the live-listen boundary.

SenseVoice itself is an offline recognizer. This adapter turns it into a
bounded-latency serving surface by decoding independent PCM windows while audio
is still arriving and force-flushing at VAD/final session boundaries. All
inference runs in the shared sync executor; the WebSocket event loop never runs
the CPU decoder directly.
"""

from __future__ import annotations

import array
import asyncio
import importlib
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional

from utils.async_tasks import create_named_task
from utils.executors import run_blocking, sync_executor
from utils.sensevoice.speaker import (
    SenseVoiceSpeakerError,
    WindowSpeakerClusterer,
    build_window_speaker_clusterer,
    sensevoice_speaker_mode,
)
from utils.stt.socket import STTSocket

logger = logging.getLogger(__name__)

SENSEVOICE_MODEL_DIR = os.getenv(
    'SENSEVOICE_MODEL_DIR',
    '/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17',
)
SENSEVOICE_THREADS = int(os.getenv('SENSEVOICE_NUM_THREADS', '4'))
SENSEVOICE_USE_ITN = os.getenv('SENSEVOICE_USE_ITN', '1') == '1'
SENSEVOICE_STREAM_WINDOW_SECONDS = float(os.getenv('SENSEVOICE_STREAM_WINDOW_SECONDS', '5.0'))
SENSEVOICE_STREAM_POLL_SECONDS = float(os.getenv('SENSEVOICE_STREAM_POLL_SECONDS', '0.1'))

_recognizer: Any = None
_recognizer_lock = threading.Lock()
_decode_lock = threading.Lock()


def sensevoice_model_is_ready(model_dir: Optional[str] = None) -> bool:
    """Return whether both files required by the runtime are mounted."""
    root = model_dir or os.getenv('SENSEVOICE_MODEL_DIR') or SENSEVOICE_MODEL_DIR
    return os.path.isfile(os.path.join(root, 'model.int8.onnx')) and os.path.isfile(os.path.join(root, 'tokens.txt'))


def get_sensevoice_recognizer() -> Any:
    """Return the process-wide sherpa-onnx recognizer, initialized lazily."""
    global _recognizer
    if _recognizer is None:
        with _recognizer_lock:
            if _recognizer is None:
                import sherpa_onnx  # pyright: ignore[reportMissingImports]

                model = os.path.join(SENSEVOICE_MODEL_DIR, "model.int8.onnx")
                tokens = os.path.join(SENSEVOICE_MODEL_DIR, "tokens.txt")
                if not (os.path.exists(model) and os.path.exists(tokens)):
                    raise RuntimeError(
                        f'SenseVoice model not found at {os.getenv("SENSEVOICE_MODEL_DIR") or SENSEVOICE_MODEL_DIR} '
                        '(model.int8.onnx and tokens.txt are required)'
                    )
                sherpa_onnx = importlib.import_module('sherpa_onnx')
                model_dir = os.getenv('SENSEVOICE_MODEL_DIR') or SENSEVOICE_MODEL_DIR
                _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=os.path.join(model_dir, 'model.int8.onnx'),
                    tokens=os.path.join(model_dir, 'tokens.txt'),
                    num_threads=SENSEVOICE_THREADS,
                    use_itn=SENSEVOICE_USE_ITN,
                )
                logger.info('SenseVoice recognizer loaded (CPU, threads=%d)', SENSEVOICE_THREADS)
    return _recognizer


def pcm16_to_samples(pcm: bytes) -> List[int]:
    aligned = pcm[: len(pcm) - (len(pcm) % 2)]
    return list(array.array('h', aligned))


def decode_pcm(recognizer: Any, sample_rate: int, pcm: bytes) -> str:
    """Decode one window under the recognizer's process-wide concurrency gate."""
    with _decode_lock:
        stream = recognizer.create_stream()
        stream.accept_waveform(sample_rate, pcm16_to_samples(pcm))
        recognizer.decode_stream(stream)
        return str(stream.result.text).strip()


class SenseVoiceSocket(STTSocket):
    """Decode bounded PCM windows and emit transcript segments incrementally."""

    def __init__(
        self,
        sample_rate: int = 16000,
        transcript_callback: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
        recognizer: Any = None,
        window_seconds: float = SENSEVOICE_STREAM_WINDOW_SECONDS,
        poll_seconds: float = SENSEVOICE_STREAM_POLL_SECONDS,
        speaker_clusterer: Optional[WindowSpeakerClusterer] = None,
    ) -> None:
        if sample_rate <= 0 or window_seconds <= 0 or poll_seconds <= 0:
            raise ValueError('SenseVoice streaming timing and sample rate must be positive')
        self._sample_rate = sample_rate
        self._callback = transcript_callback
        self._recognizer = recognizer
        mode = sensevoice_speaker_mode()
        self._speaker_clusterer = (
            speaker_clusterer
            if speaker_clusterer is not None
            else build_window_speaker_clusterer() if mode == 'window_clustering' else None
        )
        self._speaker_window_index = 0
        self._window_bytes = max(2, int(sample_rate * 2 * window_seconds))
        self._poll_seconds = poll_seconds
        self._pcm = bytearray()
        self._lock = threading.Lock()
        self._emitted_seconds = 0.0
        self._flush_requested = False
        self._closed = False
        self._dead = False
        self._death_reason: Optional[str] = None
        self._pump_task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        if self._pump_task is None:
            self._pump_task = create_named_task(self._pump(), name='sensevoice_stt_pump')

    def send(self, data: bytes) -> bool:
        if self._closed or self._dead:
            return False
        if not data:
            return True
        with self._lock:
            self._pcm.extend(data)
        return True

    def finalize(self) -> None:
        """Request an asynchronous VAD-boundary flush without blocking the loop."""
        if not self._closed and not self._dead:
            self._flush_requested = True

    def finish(self) -> None:
        self._closed = True
        self._flush_requested = True

    async def drain_and_close(self) -> None:
        self.finish()
        pump, self._pump_task = self._pump_task, None
        if pump is not None:
            try:
                await pump
            except asyncio.CancelledError:
                pass
        else:
            await self._flush(force=True)

    async def _pump(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._poll_seconds)
                closing = self._closed
                force = closing or self._flush_requested
                self._flush_requested = False
                await self._flush(force=force)
                if closing:
                    return
        except asyncio.CancelledError:
            raise
        except SenseVoiceSpeakerError as error:
            self._dead = True
            self._death_reason = f'sensevoice_speaker:{error.reason}'
            logger.error('SenseVoice speaker attribution failed reason=%s', error.reason)
        except Exception as error:
            self._dead = True
            self._death_reason = f'sensevoice_decode:{type(error).__name__}'
            logger.error('SenseVoice streaming pump failed type=%s', type(error).__name__)

    async def _flush(self, *, force: bool) -> None:
        while True:
            with self._lock:
                available = len(self._pcm) - (len(self._pcm) % 2)
                if available < self._window_bytes and not (force and available > 0):
                    return
                # A final/VAD flush may include a partial tail, but it must not
                # collapse an arbitrarily large buffered session into one CPU
                # inference or one speaker label.
                take = min(available, self._window_bytes)
                pcm = bytes(self._pcm[:take])
                del self._pcm[:take]
                start = self._emitted_seconds
                duration = take / (2 * self._sample_rate)
                self._emitted_seconds += duration

            recognizer = self._recognizer or await run_blocking(sync_executor, get_sensevoice_recognizer)
            self._recognizer = recognizer
            text = await run_blocking(
                sync_executor,
                lambda: decode_pcm(recognizer, self._sample_rate, pcm),
            )
            if self._callback and text:
                speaker = 'SPEAKER_00'
                if self._speaker_clusterer is not None:
                    speaker = await run_blocking(
                        sync_executor,
                        self._speaker_clusterer.assign_pcm,
                        self._speaker_window_index,
                        pcm,
                        self._sample_rate,
                    )
                    self._speaker_window_index += 1
                self._callback(
                    [
                        {
                            'speaker': speaker,
                            'start': start,
                            'end': start + duration,
                            'text': text,
                            'is_user': False,
                            'person_id': None,
                        }
                    ]
                )
            logger.info('SenseVoice transcript window emitted chars=%d duration=%.1fs', len(text), duration)

    @property
    def is_connection_dead(self) -> bool:
        return self._dead

    @property
    def death_reason(self) -> Optional[str]:
        return self._death_reason
