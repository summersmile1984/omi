"""MOSS (OpenMOSS) official API client: upload audio, transcribe, diarize.

Platform: https://platform.mosi.cn · API base: https://api.mosi.cn
Models:
  - ``moss-transcribe``           plain transcription -> {"text": ...}
  - ``moss-transcribe-diarize``   multi-speaker, diarize=true ->
    {"segments": [{start, end, text, speaker: "S01"}], ...}

Docs: https://platform.mosi.cn/docs/reference/transcriptions

No GPU needed — everything is server-side on MOSS. Speaker *identification*
(which person) is NOT provided by the API; it returns anonymous S01/S02
labels that the caller matches against known people via speaker embeddings.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0
POLL_INTERVAL = float(os.getenv("MOSS_POLL_INTERVAL_SECONDS", "3"))


class MossAPIError(RuntimeError):
    """Raised for MOSS API failures (auth, validation, transport)."""


def _configured_timeout() -> float:
    try:
        timeout = float(os.getenv("MOSS_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT)))
    except ValueError as error:
        raise MossAPIError("MOSS_TIMEOUT_SECONDS must be a number") from error
    if timeout <= 0:
        raise MossAPIError("MOSS_TIMEOUT_SECONDS must be positive")
    return timeout


def _configured_endpoint(value: str) -> str:
    endpoint = value.strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise MossAPIError("MOSS_API_BASE must be an explicit HTTP(S) endpoint without credentials or query")
    return endpoint


@dataclass
class MossSegment:
    """One diarized transcript segment."""

    start: float
    end: float
    text: str
    speaker: str  # "S01", "S02", ...
    segment_id: Optional[str] = None


@dataclass
class MossTranscription:
    """Full transcription + diarization result."""

    text: str
    duration: float
    segments: List[MossSegment] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


def _raise_for_error(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    try:
        detail = resp.json().get("error", {}).get("message", resp.text)
    except Exception:  # pragma: no cover - defensive
        detail = resp.text
    raise MossAPIError(f"MOSS API {resp.status_code}: {detail}")


class MossClient:
    """Thin client for the MOSS audio endpoints."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self._api_key = (api_key or os.getenv("MOSS_API_KEY", "")).strip()
        if not self._api_key:
            raise MossAPIError("MOSS_API_KEY environment variable is required")
        configured_base = base_url or os.getenv("MOSS_API_BASE", "")
        if not configured_base.strip():
            raise MossAPIError("MOSS_API_BASE environment variable is required")
        self._base = _configured_endpoint(configured_base)
        self._timeout = _configured_timeout() if timeout is None else timeout
        if self._timeout <= 0:
            raise MossAPIError("MOSS timeout must be positive")
        self._client = httpx.Client(base_url=self._base, timeout=self._timeout)

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------
    def upload_file(self, file_path: str, purpose: str = "transcription") -> str:
        """Upload an audio file; returns the file_id."""
        with open(file_path, "rb") as f:
            resp = self._client.post(
                "/v1/files",
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": (file_path.rsplit("/", 1)[-1], f)},
                data={"purpose": purpose},
            )
        _raise_for_error(resp)
        data = resp.json()
        file_id = data.get("id")
        if not file_id:
            raise MossAPIError(f"MOSS upload missing id: {data}")
        logger.info("MOSS uploaded %s -> file_id=%s", file_path, file_id)
        return file_id

    def delete_file(self, file_id: str) -> None:
        try:
            self._client.delete(f"/v1/files/{file_id}", headers={"Authorization": f"Bearer {self._api_key}"})
        except Exception:  # pragma: no cover - cleanup is best-effort
            logger.debug("MOSS delete file %s failed", file_id)

    # ------------------------------------------------------------------
    # Transcription
    # ------------------------------------------------------------------
    def transcribe(
        self,
        file_id: Optional[str] = None,
        *,
        url: Optional[str] = None,
        model: str = "moss-transcribe",
        diarize: bool = False,
        async_task: bool = False,
    ) -> MossTranscription:
        """Transcribe an uploaded file (or a public URL).

        With ``model="moss-transcribe-diarize"`` and ``diarize=True`` the
        response includes ``segments`` with anonymous ``S01/S02`` speaker
        labels (diarization). Identification is not provided by the API.
        """
        payload: Dict[str, Any] = {"model": model, "response_format": "json"}
        if file_id:
            payload["file_id"] = file_id
        if url:
            payload["url"] = url
        if model == "moss-transcribe-diarize" and diarize:
            payload["diarize"] = True

        resp = self._client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        _raise_for_error(resp)
        data = resp.json()

        if async_task:
            task_id = data.get("task_id") or data.get("id")
            return self._wait_task(task_id)

        return self._parse_transcription(data)

    def _wait_task(self, task_id: str) -> MossTranscription:
        for _ in range(120):  # ~6 min max
            resp = self._client.get(
                f"/v1/audio/tasks/{task_id}",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            _raise_for_error(resp)
            data = resp.json()
            status = data.get("status", "").upper()
            if status in ("COMPLETED", "SUCCEEDED"):
                return self._parse_transcription(data.get("result") or data)
            if status in ("FAILED", "ERROR", "CANCELLED"):
                raise MossAPIError(f"MOSS task {task_id} failed: {data}")
            time.sleep(POLL_INTERVAL)
        raise MossAPIError(f"MOSS task {task_id} timed out")

    @staticmethod
    def _parse_transcription(data: Dict[str, Any]) -> MossTranscription:
        segments = [
            MossSegment(
                start=float(seg.get("start", 0) or 0),
                end=float(seg.get("end", 0) or 0),
                text=seg.get("text", ""),
                speaker=seg.get("speaker", ""),
                segment_id=seg.get("id"),
            )
            for seg in (data.get("segments") or [])
            if seg.get("text")
        ]
        return MossTranscription(
            text=data.get("text", ""),
            duration=float(data.get("duration", 0) or 0),
            segments=segments,
            raw=data,
        )

    def close(self) -> None:
        self._client.close()
