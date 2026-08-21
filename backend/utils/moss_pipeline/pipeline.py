"""End-to-end audio pipeline: transcribe + diarize (MOSS API) -> speaker
identification (local CPU embeddings) -> person_id-annotated transcript.

Chain:
  1. MOSS ``moss-transcribe-diarize`` (diarize=true) -> segments with
     anonymous ``S01/S02`` labels + start/end + text.
  2. Slice each speaker's audio clips from the source file.
  3. Extract a speaker embedding per distinct speaker (local CPU, pluggable).
  4. Match embeddings against known people (existing
     ``utils.stt.speaker_embedding`` cosine + threshold).
  5. Map ``S01 -> person_id`` and annotate the segments.

No GPU required: MOSS does transcription+diarization server-side; speaker
identification runs on CPU via a pluggable embedding extractor (wespeaker /
pyannote), defaulting to the deployment-selected HTTP embedding API.
"""

from __future__ import annotations

import io
import logging
import wave
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

import numpy as np

from .moss_client import MlxAudioClient, MossClient, MossSegment, MossTranscription, create_moss_client

logger = logging.getLogger(__name__)

MIN_SPEAKER_CLIP_SECONDS = 1.0


@dataclass
class AnnotatedSegment(MossSegment):
    """A diarized segment with optional person attribution."""

    person_id: Optional[str] = None
    person_name: Optional[str] = None
    matched: bool = False


@dataclass
class PipelineResult:
    """Full chain output."""

    transcript: MossTranscription
    segments: List[AnnotatedSegment] = field(default_factory=list)
    speaker_map: Dict[str, Tuple[str, str]] = field(default_factory=dict)  # S01 -> (person_id, name)
    matched_speakers: int = 0
    total_speakers: int = 0


EmbeddingExtractor = Callable[[bytes], np.ndarray]


def _wav_slice(wav_bytes: bytes, start_sec: float, end_sec: float) -> Optional[bytes]:
    """Slice a mono 16-bit WAV between start_sec and end_sec (inclusive)."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            sr = wf.getframerate()
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            start_frame = int(start_sec * sr) * nch
            end_frame = int(end_sec * sr) * nch
            wf.setpos(start_frame)
            frames = wf.readframes(end_frame - start_frame)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setnchannels(nch)
            out.setsampwidth(sw)
            out.setframerate(sr)
            out.writeframes(frames)
        return buf.getvalue()
    except Exception as exc:  # pragma: no cover - malformed wav
        logger.warning("wav slice failed (%s-%s): %s", start_sec, end_sec, exc)
        return None


def _default_embedding_extractor() -> EmbeddingExtractor:
    """Embedding extractor backed by the configured HTTP embedding API.

    Returns a callable ``(wav_bytes) -> (1, D) np.ndarray``. The configured
    endpoint (``SPEAKER_EMBEDDING_API_URL``) is the diarizer's
    ``/v2/embedding`` (wespeaker resnet34) — works with or without a local
    GPU because the diarizer service falls back to CPU.
    """
    from utils.stt.speaker_embedding import extract_embedding_from_bytes

    return lambda data: extract_embedding_from_bytes(data, "moss_speaker.wav")


def _default_matcher() -> Callable[[np.ndarray, np.ndarray], float]:
    """Return the embedding distance function (cosine; lower = closer)."""
    from utils.stt.speaker_embedding import compare_embeddings

    return compare_embeddings


class MossSpeakerPipeline:
    """Transcribe + diarize (MOSS) then identify speakers (local CPU)."""

    def __init__(
        self,
        *,
        client: Optional[MossClient | MlxAudioClient] = None,
        embedding_extractor: Optional[EmbeddingExtractor] = None,
        matcher: Optional[Callable[[np.ndarray, np.ndarray], float]] = None,
        match_threshold: float = 0.45,
        min_clip_seconds: float = MIN_SPEAKER_CLIP_SECONDS,
    ) -> None:
        self._client = client or create_moss_client()
        self._extract_embedding = embedding_extractor or _default_embedding_extractor()
        self._compare = matcher or _default_matcher()
        self._threshold = match_threshold
        self._min_clip = min_clip_seconds

    # ------------------------------------------------------------------
    def run(
        self,
        wav_bytes: bytes,
        people_embeddings: Dict[str, Dict[str, Any]],
        *,
        transcribe_model: str = "moss-transcribe-diarize",
    ) -> PipelineResult:
        """Run the full chain on in-memory WAV bytes.

        Args:
            wav_bytes: mono 16-bit WAV audio.
            people_embeddings: {person_id: {"name": str, "embedding": (1,D)}}
            transcribe_model: "moss-transcribe" or "moss-transcribe-diarize"

        Returns:
            PipelineResult with segments annotated with person_id/name.
        """
        return self.run_file_upload(
            file_path=None,
            wav_bytes=wav_bytes,
            people_embeddings=people_embeddings,
            transcribe_model=transcribe_model,
        )

    def run_file_upload(
        self,
        *,
        file_path: Optional[str] = None,
        wav_bytes: Optional[bytes] = None,
        people_embeddings: Dict[str, Dict[str, Any]],
        transcribe_model: str = "moss-transcribe-diarize",
    ) -> PipelineResult:
        # 1) Upload + transcribe + diarize via MOSS
        if isinstance(self._client, MlxAudioClient):
            if file_path:
                with open(file_path, 'rb') as source_file:
                    source_bytes = source_file.read()
                filename = file_path.rsplit('/', 1)[-1] or 'audio.wav'
            elif wav_bytes is not None:
                source_bytes = wav_bytes
                filename = 'audio.wav'
            else:
                raise ValueError('file_path or wav_bytes is required')
            # mlx-audio owns the model configured in MOSS_MODEL; it has no
            # MOSS file/task upload protocol.
            transcription = self._client.transcribe_audio(
                source_bytes,
                filename=filename,
                diarize=True,
            )
        elif file_path:
            file_id = self._client.upload_file(file_path)
            try:
                transcription = self._client.transcribe(file_id=file_id, model=transcribe_model, diarize=True)
            finally:
                self._client.delete_file(file_id)
        else:
            # in-memory: write temp file for MOSS upload
            import tempfile

            if wav_bytes is None:
                raise ValueError('file_path or wav_bytes is required')
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
                tmp.write(cast(bytes, wav_bytes))
                tmp.flush()
                file_id = self._client.upload_file(tmp.name)
                try:
                    transcription = self._client.transcribe(file_id=file_id, model=transcribe_model, diarize=True)
                finally:
                    self._client.delete_file(file_id)

        # 2) Group segments by speaker label, pick longest clip per speaker
        by_speaker: Dict[str, List[MossSegment]] = {}
        for seg in transcription.segments:
            by_speaker.setdefault(seg.speaker, []).append(seg)
        result = PipelineResult(
            transcript=transcription,
            segments=[AnnotatedSegment(**vars(s)) for s in transcription.segments],
        )
        result.total_speakers = len(by_speaker)

        # 3) Identify each speaker against known people
        matched_ids: set = set()
        for speaker, segs in sorted(by_speaker.items(), key=lambda kv: -max(s.end - s.start for s in kv[1])):
            best_seg = max(segs, key=lambda s: s.end - s.start)
            if best_seg.end - best_seg.start < self._min_clip:
                continue
            if file_path:
                clip = _wav_slice_from_file(file_path, best_seg.start, best_seg.end)
            else:
                if wav_bytes is None:
                    raise ValueError('file_path or wav_bytes is required')
                clip = _wav_slice(wav_bytes, best_seg.start, best_seg.end)
            if not clip:
                continue
            try:
                query = self._extract_embedding(clip)
            except Exception as exc:  # pragma: no cover - extraction failure
                logger.info("speaker %s embedding failed: %s", speaker, exc)
                continue

            best_match, best_dist = None, float("inf")
            for person_id, person_data in people_embeddings.items():
                if person_id in matched_ids:
                    continue
                distance = self._compare(query, person_data["embedding"])
                if distance < best_dist:
                    best_dist, best_match = distance, (person_id, person_data.get("name", person_id))
            if best_match and best_dist < self._threshold:
                person_id, person_name = best_match
                result.speaker_map[speaker] = (person_id, person_name)
                matched_ids.add(person_id)
                result.matched_speakers += 1
                logger.info("speaker %s -> %s (distance=%.3f)", speaker, person_name, best_dist)

        # 4) Annotate segments
        for seg in result.segments:
            if seg.speaker in result.speaker_map:
                seg.person_id, seg.person_name = result.speaker_map[seg.speaker]
                seg.matched = True
        return result

    def close(self) -> None:
        self._client.close()


def _wav_slice_from_file(path: str, start_sec: float, end_sec: float) -> Optional[bytes]:
    with open(path, "rb") as f:
        data = f.read()
    return _wav_slice(data, start_sec, end_sec)
