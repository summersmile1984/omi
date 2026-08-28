import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from voice_transcription_routes import (  # noqa: E402
    MAX_AUDIO_BYTES,
    transcribe_voice_message,
)


class FakeAi:
    def __init__(self, results=None, error=None):
        self.results = list(results or [{"text": "hello", "transcription_info": {"language": "en"}}])
        self.error = error
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        if self.error:
            raise self.error
        return self.results.pop(0)


class FakeRequest:
    def __init__(self, env, headers=None, body=b"", query=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self._body = body
        self.query_params = query or {}

    async def body(self):
        return self._body


def signed_headers(secret: str, *, content_type: str) -> dict[str, str]:
    raw = json.dumps({"uid": "voice-user"}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
        "content-type": content_type,
    }


def env(secret: str, ai=None):
    attributes = {"INTERNAL_ASSERTION_SECRET": secret}
    if ai is not None:
        attributes["AI"] = ai
    return type("Env", (), attributes)()


def multipart(parts, boundary="voice-boundary"):
    body = bytearray()
    for name, value, filename, content_type in parts:
        body.extend(f"--{boundary}\r\n".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        body.extend(f"{disposition}\r\n".encode())
        if content_type:
            body.extend(f"Content-Type: {content_type}\r\n".encode())
        body.extend(b"\r\n")
        body.extend(value if isinstance(value, bytes) else value.encode())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def wav(pcm=b"\x00\x00"):
    return b"".join(
        (
            b"RIFF",
            struct.pack("<I", len(pcm) + 36),
            b"WAVEfmt ",
            struct.pack("<IHHIIHH", 16, 1, 1, 16_000, 32_000, 2, 16),
            b"data",
            struct.pack("<I", len(pcm)),
            pcm,
        )
    )


def test_multipart_voice_message_uses_workers_ai_and_preserves_response_contract():
    secret = "voice-secret"
    ai = FakeAi(
        [
            {"text": "first", "transcription_info": {"language": "en"}},
            {"text": "second", "transcription_info": {"language": "en"}},
        ]
    )
    body, content_type = multipart(
        [
            ("language", "en", None, None),
            ("files", wav(b"\x00\x00"), "first.wav", "audio/wav"),
            ("files", b"\x1a\x45\xdf\xa3webm", "second.webm", "audio/webm"),
        ]
    )
    response = asyncio.run(
        transcribe_voice_message(FakeRequest(env(secret, ai), signed_headers(secret, content_type=content_type), body))
    )

    assert response == {
        "transcript": "first second",
        "stt_provider": "workers-ai",
        "stt_model": "@cf/openai/whisper-large-v3-turbo",
        "outcome": "success",
        "language": "en",
    }
    assert len(ai.calls) == 2
    assert ai.calls[0][1] == {
        "audio": base64.b64encode(wav(b"\x00\x00")).decode("ascii"),
        "task": "transcribe",
        "vad_filter": True,
        "language": "en",
    }
    assert base64.b64decode(ai.calls[1][1]["audio"]) == b"\x1a\x45\xdf\xa3webm"


def test_octet_stream_wraps_linear16_pcm_in_wav_and_omits_auto_language():
    secret = "voice-secret"
    ai = FakeAi([{"text": "desktop"}])
    response = asyncio.run(
        transcribe_voice_message(
            FakeRequest(
                env(secret, ai),
                signed_headers(secret, content_type="application/octet-stream"),
                b"\x01\x00\x02\x00",
                {"encoding": "linear16", "sample_rate": "16000", "channels": "1", "language": "multi"},
            )
        )
    )

    assert response["transcript"] == "desktop"
    assert "language" not in response
    payload = ai.calls[0][1]
    audio = base64.b64decode(payload["audio"])
    assert audio.startswith(b"RIFF")
    assert audio[8:12] == b"WAVE"
    assert audio[44:] == b"\x01\x00\x02\x00"
    assert "language" not in payload


def test_expected_silence_and_multiple_detected_languages_are_normalized():
    secret = "voice-secret"
    ai = FakeAi(
        [
            {"text": "", "transcription_info": {"language": "en"}},
            {"text": " ", "detected_language": "fr"},
        ]
    )
    body, content_type = multipart(
        [
            ("files", wav(), "first.wav", "audio/wav"),
            ("files", wav(), "second.wav", "audio/wav"),
        ]
    )
    response = asyncio.run(
        transcribe_voice_message(FakeRequest(env(secret, ai), signed_headers(secret, content_type=content_type), body))
    )

    assert response["transcript"] == ""
    assert response["outcome"] == "expected_silence"
    assert response["language"] == "multi"


def test_voice_message_rejects_unauthenticated_malformed_and_oversized_input_before_inference():
    secret = "voice-secret"
    ai = FakeAi()
    unauthorized = asyncio.run(
        transcribe_voice_message(
            FakeRequest(env(secret, ai), {"content-type": "application/octet-stream"}, b"\x00\x00")
        )
    )
    malformed_body, content_type = multipart([("files", b"not a wav", "audio.wav", "audio/wav")])
    malformed = asyncio.run(
        transcribe_voice_message(
            FakeRequest(env(secret, ai), signed_headers(secret, content_type=content_type), malformed_body)
        )
    )
    oversized_headers = signed_headers(secret, content_type="application/octet-stream")
    oversized_headers["content-length"] = str(MAX_AUDIO_BYTES + 1)
    oversized = asyncio.run(transcribe_voice_message(FakeRequest(env(secret, ai), oversized_headers, b"\x00\x00")))

    assert unauthorized.status_code == 401
    assert malformed.status_code == 400
    assert json.loads(malformed.body)["detail"]["outcome"] == "invalid_input"
    assert oversized.status_code == 413
    assert ai.calls == []


def test_voice_message_validates_pcm_shape_language_and_content_type():
    secret = "voice-secret"
    ai = FakeAi()
    headers = signed_headers(secret, content_type="application/octet-stream")
    cases = [
        (b"\x00", {}),
        (b"\x00\x00", {"sample_rate": "7999"}),
        (b"\x00\x00", {"channels": "3"}),
        (b"\x00\x00", {"encoding": "mulaw"}),
        (b"\x00\x00", {"language": "not_a_language"}),
    ]
    for body, query in cases:
        response = asyncio.run(transcribe_voice_message(FakeRequest(env(secret, ai), headers, body, query)))
        assert response.status_code == 400
    unsupported = asyncio.run(
        transcribe_voice_message(
            FakeRequest(
                env(secret, ai),
                signed_headers(secret, content_type="audio/wav"),
                wav(),
            )
        )
    )
    assert unsupported.status_code == 415
    assert ai.calls == []


def test_voice_message_returns_stable_configuration_and_provider_failures():
    secret = "voice-secret"
    body, content_type = multipart([("files", wav(), "audio.wav", "audio/wav")])
    headers = signed_headers(secret, content_type=content_type)

    missing = asyncio.run(transcribe_voice_message(FakeRequest(env(secret), headers, body)))
    unavailable = asyncio.run(
        transcribe_voice_message(FakeRequest(env(secret, FakeAi(error=RuntimeError("sensitive"))), headers, body))
    )
    invalid = asyncio.run(
        transcribe_voice_message(FakeRequest(env(secret, FakeAi(results=[{"unexpected": True}])), headers, body))
    )

    assert missing.status_code == 503
    assert json.loads(missing.body)["detail"] == {
        "error": "stt_provider_configuration_error",
        "outcome": "config_error",
        "provider": "workers-ai",
        "retryable": False,
        "message": "The transcription provider is temporarily unavailable.",
    }
    for response in (unavailable, invalid):
        assert response.status_code == 502
        assert json.loads(response.body)["detail"]["error"] == "stt_upstream_error"
        assert b"sensitive" not in response.body
