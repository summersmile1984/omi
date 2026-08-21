"""MOSS-compatible clients for operator-owned batch transcription.

The default wire is the OpenMOSS file/task API, but its authority is always
operator-configured.  ``MlxAudioClient`` is a separate adapter for the
operator-owned mlx-audio server, whose multipart wire does not implement the
MOSS file-upload API.
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
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

from .config import (
    MOSS_TRANSPORT_MLX_AUDIO,
    MOSS_TRANSPORT_MOSI,
    MossConfigurationError,
    MossRuntimeConfig,
    resolve_moss_config,
    resolve_moss_timeout,
    validate_moss_audio_url,
    validate_moss_base_url,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0
POLL_INTERVAL = 3.0


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
    """Client for the operator's OpenMOSS file/task endpoints."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        runtime_config: Optional[MossRuntimeConfig] = None,
    ) -> None:
        try:
            config = runtime_config or resolve_moss_config(
                api_key=api_key,
                base_url=base_url,
                transport=MOSS_TRANSPORT_MOSI,
            )
        except MossConfigurationError as exc:
            raise MossAPIError(str(exc)) from exc
        if config.transport != MOSS_TRANSPORT_MOSI:
            raise MossAPIError('MossClient requires MOSS_TRANSPORT=mosi')
        self._api_key = config.api_key
        self._base = validate_moss_base_url(config.base_url)
        try:
            self._timeout = resolve_moss_timeout(timeout)
        except MossConfigurationError as exc:
            raise MossAPIError(str(exc)) from exc
        # Do not follow a redirect to an authority that was not validated.
        self._client = httpx.Client(base_url=self._base, timeout=self._timeout, follow_redirects=False)

    def _validate_transport(self) -> None:
        """Recheck the immutable authority immediately before each HTTP call."""

        try:
            validate_moss_base_url(self._base)
        except MossConfigurationError as exc:
            raise MossAPIError(str(exc)) from exc

    def _auth_headers(self) -> Dict[str, str]:
        self._validate_transport()
        return {"Authorization": f"Bearer {self._api_key}"}

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------
    def upload_file(self, file_path: str, purpose: str = "transcription") -> str:
        """Upload an audio file; returns the file_id."""
        self._validate_transport()
        with open(file_path, "rb") as f:
            resp = self._client.post(
                "/v1/files",
                headers=self._auth_headers(),
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
            self._validate_transport()
            self._client.delete(f"/v1/files/{file_id}", headers=self._auth_headers())
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
        self._validate_transport()
        if url:
            try:
                url = validate_moss_audio_url(url)
            except MossConfigurationError as exc:
                raise MossAPIError(str(exc)) from exc
        payload: Dict[str, Any] = {"model": model, "response_format": "json"}
        if file_id:
            payload["file_id"] = file_id
        if url:
            payload["url"] = url
        if model == "moss-transcribe-diarize" and diarize:
            payload["diarize"] = True

        resp = self._client.post(
            "/v1/audio/transcriptions",
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        _raise_for_error(resp)
        data = resp.json()

        if async_task:
            task_id = data.get("task_id") or data.get("id")
            return self._wait_task(task_id)

        return self.parse_transcription(data)

    def _wait_task(self, task_id: str) -> MossTranscription:
        for _ in range(120):  # ~6 min max
            self._validate_transport()
            resp = self._client.get(
                f"/v1/audio/tasks/{task_id}",
                headers=self._auth_headers(),
            )
            _raise_for_error(resp)
            data = resp.json()
            status = data.get("status", "").upper()
            if status in ("COMPLETED", "SUCCEEDED"):
                return self.parse_transcription(data.get("result") or data)
            if status in ("FAILED", "ERROR", "CANCELLED"):
                raise MossAPIError(f"MOSS task {task_id} failed: {data}")
            time.sleep(POLL_INTERVAL)
        raise MossAPIError(f"MOSS task {task_id} timed out")

    @staticmethod
    def parse_transcription(data: Dict[str, Any]) -> MossTranscription:
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


class MlxAudioClient:
    """Adapter for an operator-owned mlx-audio OpenAI-compatible server.

    mlx-audio exposes multipart /v1/audio/transcriptions directly. It does
    not implement the MOSS /v1/files or asynchronous task endpoints, so it
    is deliberately kept separate from MossClient instead of pretending the
    two wires are interchangeable.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        runtime_config: Optional[MossRuntimeConfig] = None,
    ) -> None:
        try:
            config = runtime_config or resolve_moss_config(
                api_key=api_key,
                base_url=base_url,
                transport=MOSS_TRANSPORT_MLX_AUDIO,
                model=model,
            )
        except MossConfigurationError as exc:
            raise MossAPIError(str(exc)) from exc
        if config.transport != MOSS_TRANSPORT_MLX_AUDIO:
            raise MossAPIError('MlxAudioClient requires MOSS_TRANSPORT=mlx_audio')
        self._api_key = config.api_key
        self._base = validate_moss_base_url(config.base_url)
        self._model = config.model
        try:
            self._timeout = resolve_moss_timeout(timeout)
        except MossConfigurationError as exc:
            raise MossAPIError(str(exc)) from exc
        self._client = httpx.Client(base_url=self._base, timeout=self._timeout, follow_redirects=False)

    def _validate_transport(self) -> None:
        try:
            validate_moss_base_url(self._base)
        except MossConfigurationError as exc:
            raise MossAPIError(str(exc)) from exc

    def _auth_headers(self) -> Dict[str, str]:
        self._validate_transport()
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def list_models(self) -> List[str]:
        """Return model IDs advertised by the operator endpoint."""

        self._validate_transport()
        resp = self._client.get('/v1/models', headers=self._auth_headers())
        _raise_for_error(resp)
        try:
            data = resp.json()
            models = [str(item['id']) for item in data.get('data', []) if item.get('id')]
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise MossAPIError('mlx-audio /v1/models returned an invalid response') from exc
        if not models:
            raise MossAPIError('mlx-audio /v1/models returned no models')
        return models

    def transcribe_audio(
        self,
        audio_bytes: bytes,
        *,
        filename: str = 'audio.wav',
        language: Optional[str] = None,
        context: Optional[str] = None,
        diarize: bool = True,
    ) -> MossTranscription:
        """Transcribe bytes using mlx-audio's multipart OpenAI-compatible wire."""

        self._validate_transport()
        data: Dict[str, str] = {
            'model': self._model,
            'response_format': 'verbose_json',
        }
        if language:
            data['language'] = language
        if context:
            data['context'] = context
        if diarize:
            data['diarize'] = 'true'
        resp = self._client.post(
            '/v1/audio/transcriptions',
            headers=self._auth_headers(),
            files={'file': (filename or 'audio.wav', audio_bytes, 'audio/wav')},
            data=data,
        )
        _raise_for_error(resp)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise MossAPIError('mlx-audio transcription returned invalid JSON') from exc
        if not isinstance(payload, dict):
            raise MossAPIError('mlx-audio transcription returned an invalid object')
        return MossClient.parse_transcription(payload)

    def close(self) -> None:
        self._client.close()


def download_audio_url(audio_url: str, *, timeout: Optional[float] = None) -> bytes:
    """Fetch one caller URL after SSRF/vendor validation, without redirects."""

    try:
        validated_url = validate_moss_audio_url(audio_url)
        request_timeout = resolve_moss_timeout(timeout)
    except MossConfigurationError as exc:
        raise MossAPIError(str(exc)) from exc
    try:
        with httpx.Client(timeout=request_timeout, follow_redirects=False) as client:
            resp = client.get(validated_url)
    except httpx.HTTPError as exc:
        raise MossAPIError(f'MOSS audio download failed: {type(exc).__name__}') from exc
    _raise_for_error(resp)
    return resp.content


def create_moss_client(
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> MossClient | MlxAudioClient:
    """Build the explicitly selected MOSS-compatible transport adapter."""

    try:
        config = resolve_moss_config(
            api_key=api_key,
            base_url=base_url,
            transport=None,
            model=model,
        )
    except MossConfigurationError as exc:
        raise MossAPIError(str(exc)) from exc
    if config.transport == MOSS_TRANSPORT_MLX_AUDIO:
        return MlxAudioClient(timeout=timeout, runtime_config=config)
    return MossClient(timeout=timeout, runtime_config=config)
