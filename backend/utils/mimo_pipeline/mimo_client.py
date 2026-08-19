"""MiMo-V2.5-ASR official API client: transcribe audio via OpenAI-compatible
chat completions.

Platform: https://mimo.mi.com · API base: https://api.xiaomimimo.com
(China TokenPlan base: https://token-plan-cn.xiaomimimo.com, same /v1 shape)
Model: ``mimo-v2.5-asr``

MiMo-V2.5-ASR is an OpenAI-compatible **chat completions** endpoint (NOT
``/v1/audio/transcriptions``): the audio is passed as base64 in an
``input_audio`` content part and the transcript comes back in
``choices.message.content``. It handles Mandarin + English + Chinese dialects
+ code-switching + noisy/multi-speaker audio on Xiaomi's side — no local GPU.

Docs: https://mimo.mi.com/docs/zh-CN/api/audio/Speech-Recognition

No GPU needed — everything is server-side on MiMo. Diarization (S01/S02) is
NOT returned by the ASR API; caller-facing speaker identity stays the job of
the MOSS pipeline / local wespeaker matching.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# OpenAI-compatible base for MiMo ASR. MIMO_API_BASE overrides for a custom
# gateway; MIMO_USE_TOKENPLAN=1 switches to the China TokenPlan base.
MIMO_API_BASE = os.getenv("MIMO_API_BASE", "https://api.xiaomimimo.com")
MIMO_TOKENPLAN_BASE = os.getenv("MIMO_TOKENPLAN_BASE", "https://token-plan-cn.xiaomimimo.com")
MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")
MIMO_ASR_MODEL = os.getenv("MIMO_ASR_MODEL", "mimo-v2.5-asr")
DEFAULT_TIMEOUT = float(os.getenv("MIMO_TIMEOUT_SECONDS", "120"))

# wav/mp3 <= 10MB per MiMo docs; keep a conservative cap.
MAX_AUDIO_BYTES = 10 * 1024 * 1024


class MimoAPIError(RuntimeError):
    """Raised for MiMo ASR API failures (auth, validation, transport)."""


@dataclass
class MimoSegment:
    """One transcript segment (MiMo returns plain text, so a single segment)."""

    start: float
    end: float
    text: str
    speaker: str  # "SPEAKER_00" — ASR API does not diarize


@dataclass
class MimoTranscription:
    """Full transcription result."""

    text: str
    duration: float
    segments: List[MimoSegment] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


def _resolve_base_url() -> str:
    if os.getenv("MIMO_USE_TOKENPLAN", "").strip().lower() in ("1", "true", "yes"):
        return MIMO_TOKENPLAN_BASE
    return MIMO_API_BASE


def _raise_for_error(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    try:
        detail = resp.json().get("error", {}).get("message", resp.text)
    except Exception:  # pragma: no cover - defensive
        detail = resp.text
    raise MimoAPIError(f"MiMo ASR API {resp.status_code}: {detail}")


def _guess_format(path_or_name: str, content_type: Optional[str] = None) -> str:
    """Map a file suffix / content type to the MiMo format token."""
    if content_type:
        ct = content_type.lower()
        if "mpeg" in ct or "mp3" in ct:
            return "mp3"
        if "wav" in ct or "wave" in ct:
            return "wav"
    name = (path_or_name or "").lower()
    if name.endswith(".mp3"):
        return "mp3"
    if name.endswith(".wav"):
        return "wav"
    if name.endswith(".m4a"):
        return "m4a"
    if name.endswith(".flac"):
        return "flac"
    if name.endswith(".ogg"):
        return "ogg"
    return "wav"


class MimoClient:
    """Thin client for MiMo-V2.5-ASR via OpenAI-compatible chat completions."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key or MIMO_API_KEY
        self._base_url = (base_url or _resolve_base_url()).rstrip("/")
        self._model = model or MIMO_ASR_MODEL
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        if not self._api_key:
            raise MimoAPIError("MIMO_API_KEY environment variable not set")
        return {"Authorization": f"Bearer {self._api_key}"}

    def _endpoint(self) -> str:
        return f"{self._base_url}/v1/chat/completions"

    def _build_messages(
        self,
        audio_b64: str,
        audio_format: str,
        language: Optional[str] = None,
        instruction: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        # Official quick-start uses a data URL: data:{MIME_TYPE};base64,...
        mime = "audio/wav" if audio_format == "wav" else "audio/mpeg"
        content: List[Dict[str, Any]] = [
            {
                "type": "input_audio",
                "input_audio": {
                    "data": f"data:{mime};base64,{audio_b64}",
                },
            }
        ]
        if instruction:
            content.append({"type": "text", "text": instruction})
        return [{"role": "user", "content": content}]

    def transcribe_audio(
        self,
        audio_bytes: bytes,
        *,
        audio_format: Optional[str] = None,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        language: Optional[str] = None,
        instruction: Optional[str] = None,
    ) -> MimoTranscription:
        """Transcribe raw audio bytes. Returns a MimoTranscription whose
        ``text`` is the plain transcript (single segment)."""
        if len(audio_bytes) > MAX_AUDIO_BYTES:
            raise MimoAPIError(
                f"audio too large for MiMo ASR ({len(audio_bytes)} > {MAX_AUDIO_BYTES} bytes); "
                "MiMo docs cap audio at 10MB — chunk it upstream"
            )
        fmt = _guess_format(filename or "", content_type) if audio_format is None else audio_format
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        messages = self._build_messages(audio_b64, fmt, language=language, instruction=instruction)
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }
        # Official quick-start passes the language via asr_options.
        if language:
            payload["asr_options"] = {"language": language}
        resp = httpx.post(
            self._endpoint(),
            headers=self._headers(),
            json=payload,
            timeout=self._timeout,
        )
        _raise_for_error(resp)
        data = resp.json()
        text = self._extract_text(data)
        return MimoTranscription(
            text=text,
            duration=0.0,  # MiMo ASR does not return duration; caller measures
            segments=([MimoSegment(start=0.0, end=0.0, text=text, speaker="SPEAKER_00")] if text else []),
            raw=data,
        )

    @staticmethod
    def _extract_text(data: Dict[str, Any]) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise MimoAPIError(f"unexpected MiMo ASR response shape: {json.dumps(data)[:300]}")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text", ""))
            return " ".join(p for p in parts if p).strip()
        return (content or "").strip()
