"""MiMo-V2.5-TTS synthesis client for the cloud-neutral Omi fork.

MiMo-V2.5-TTS is an operator-configured OpenAI-compatible chat completions endpoint (NOT
``/v1/audio/speech``): the text goes in the ``assistant`` message and the
audio comes back base64-encoded in ``choices.message.audio.data``.

Request shape (official quick-start):
  model   = "mimo-v2.5-tts"
  messages = [{role: user, content: ""}, {role: assistant, content: <text>}]
  audio   = {format: "wav"|"pcm16", voice: "冰糖"|"茉莉"|"苏打"|"白桦"|...}

Returns a WAV (24kHz mono) by default. Voice cloning / voice design use the
``mimo-v2.5-tts-voiceclone`` / ``mimo-v2.5-tts-voicedesign`` models.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, Optional

import httpx

from .config import MimoConfigurationError, resolve_mimo_config, timeout_seconds

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0


class MimoTTSAPIError(RuntimeError):
    """Raised for MiMo TTS API failures (auth, validation, transport)."""


class MimoTTSClient:
    """Thin client for MiMo-V2.5-TTS via OpenAI-compatible chat completions."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        voice: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        try:
            config = resolve_mimo_config(api_key=api_key, base_url=base_url)
            request_timeout = timeout_seconds(DEFAULT_TIMEOUT) if timeout is None else timeout
        except MimoConfigurationError as exc:
            raise MimoTTSAPIError(str(exc)) from exc
        if request_timeout <= 0:
            raise MimoTTSAPIError("MIMO_TIMEOUT_SECONDS must be greater than zero")
        self._api_key = config.api_key
        self._base_url = config.base_url
        self._model = model or os.getenv("MIMO_TTS_MODEL", "mimo-v2.5-tts").strip() or "mimo-v2.5-tts"
        self._voice = voice or os.getenv("MIMO_TTS_VOICE", "冰糖").strip() or "冰糖"
        self._timeout = request_timeout

    def _endpoint(self) -> str:
        return f"{self._base_url}/v1/chat/completions"

    def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        audio_format: Optional[str] = None,
        style_instruction: Optional[str] = None,
    ) -> bytes:
        """Synthesize speech for ``text``. Returns raw audio bytes (WAV).

        ``style_instruction`` (optional) is a natural-language style prompt
        passed as the user message (e.g. "用轻快上扬的语调，语速稍快").
        """
        if not text.strip():
            raise MimoTTSAPIError("empty TTS text")
        fmt = audio_format or os.getenv("MIMO_TTS_FORMAT", "wav").strip() or "wav"
        voice_id = voice or self._voice
        messages: list[Dict[str, Any]] = []
        if style_instruction:
            messages.append({"role": "user", "content": style_instruction})
        else:
            messages.append({"role": "user", "content": ""})
        messages.append({"role": "assistant", "content": text})

        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "audio": {"format": fmt, "voice": voice_id},
        }
        resp = httpx.post(
            self._endpoint(),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self._timeout,
        )
        if not resp.is_success:
            try:
                detail = resp.json().get("error", {}).get("message", resp.text)
            except Exception:  # pragma: no cover - defensive
                detail = resp.text
            raise MimoTTSAPIError(f"MiMo TTS API {resp.status_code}: {detail}")

        data = resp.json()
        try:
            audio_b64 = data["choices"][0]["message"]["audio"]["data"]
        except (KeyError, IndexError, TypeError):
            raise MimoTTSAPIError(f"unexpected MiMo TTS response shape: {str(data)[:300]}")
        if not audio_b64:
            raise MimoTTSAPIError("MiMo TTS returned empty audio")
        return base64.b64decode(audio_b64)
