import base64
import asyncio
import hashlib
import hmac
import json
import sys
from urllib.parse import urlsplit
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import entry  # noqa: E402

from entry import _provider_url, transcribe, transcribe_workers_ai, tts_synthesize  # noqa: E402


class FakeRequest:
    def __init__(self, env, headers, json_body=None, *, method="POST", url="https://api.test/v1/tts/synthesize"):
        self.scope = {"env": env}
        self.headers = headers
        self.json_body = json_body or {"input": "hello"}
        self.method = method
        parsed_url = urlsplit(url)
        self.url = type("Url", (), {"path": parsed_url.path, "query": parsed_url.query})()

    async def body(self):
        return b"audio"

    async def json(self):
        return self.json_body


def signed_context(secret: str) -> tuple[str, str]:
    raw = json.dumps({"uid": "user-1"}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return encoded, base64.urlsafe_b64encode(signature).decode().rstrip("=")


def test_provider_url_normalizes_slashes():
    assert _provider_url("https://asr.example.test/", "/v2/transcribe") == "https://asr.example.test/v2/transcribe"


def test_transcribe_fails_closed_when_provider_is_missing():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
    )

    response = asyncio.run(transcribe(request))

    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "transcription provider is not configured"}


def test_transcribe_uses_worker_fetch_for_provider(monkeypatch):
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(
            INTERNAL_ASSERTION_SECRET=secret,
            ASR_API_BASE_URL="https://asr.example.test",
            ASR_API_KEY="key",
        ),
        {
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
            "content-type": "multipart/form-data; boundary=test",
            "content-length": "5",
        },
    )

    class FakeResponse:
        status = 200
        headers = {"content-type": "application/json"}

        async def json(self):
            return {"text": "hello"}

    calls = {}

    async def fake_fetch(url, **options):
        calls["url"] = url
        calls["options"] = options
        return FakeResponse()

    monkeypatch.setattr(entry, "worker_fetch", fake_fetch)
    response = asyncio.run(transcribe(request))

    assert response.status_code == 200
    assert json.loads(response.body) == {"text": "hello"}
    assert calls["url"] == "https://asr.example.test/v2/transcribe"
    assert calls["options"]["body"] == b"audio"


def test_workers_ai_transcribe_uses_native_binding_and_normalizes_result():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    calls = {}

    class FakeAI:
        async def run(self, model, payload):
            calls["model"] = model
            calls["payload"] = payload
            return {"text": "hello", "word_count": 1, "vtt": "WEBVTT"}

    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, AI=FakeAI(), WORKERS_AI_ASR_MODEL="@cf/openai/whisper"),
        {
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
            "content-type": "audio/wav",
            "content-length": "5",
        },
    )

    response = asyncio.run(transcribe_workers_ai(request))

    assert response == {
        "text": "hello",
        "segments": [],
        "detected_language": None,
        "provider": "workers-ai",
        "model": "@cf/openai/whisper",
        "word_count": 1,
        "vtt": "WEBVTT",
    }
    assert calls == {"model": "@cf/openai/whisper", "payload": {"audio": [97, 117, 100, 105, 111]}}


def test_workers_ai_transcribe_rejects_multipart_contract():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, AI=object()),
        {
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
            "content-type": "multipart/form-data; boundary=test",
        },
    )

    response = asyncio.run(transcribe_workers_ai(request))

    assert response.status_code == 415
    assert json.loads(response.body) == {"error": "workers ai transcription expects a raw audio body"}


def test_workers_ai_transcribe_fails_closed_without_binding():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret),
        {
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
            "content-type": "audio/wav",
        },
    )

    response = asyncio.run(transcribe_workers_ai(request))

    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "workers ai is not configured"}


def test_embeddings_uses_worker_fetch_for_provider(monkeypatch):
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(
            INTERNAL_ASSERTION_SECRET=secret,
            EMBEDDING_API_BASE_URL="https://embedding.example.test/",
            EMBEDDING_API_KEY="key",
            EMBEDDING_MODEL="embed-model",
        ),
        {
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
        },
    )

    class FakeResponse:
        status = 200

        async def json(self):
            return {"data": [{"embedding": [0.1, 0.2]}]}

    calls = {}

    async def fake_fetch(url, **options):
        calls["url"] = url
        calls["options"] = options
        return FakeResponse()

    monkeypatch.setattr(entry, "worker_fetch", fake_fetch)
    response = asyncio.run(entry.embeddings(request))

    assert response.status_code == 200
    assert json.loads(response.body) == {"data": [{"embedding": [0.1, 0.2]}]}
    assert calls["url"] == "https://embedding.example.test/v1/embeddings"
    assert json.loads(calls["options"]["body"]) == {"model": "embed-model", "input": "hello"}


def test_ai_proxy_maps_path_and_query_to_fixed_provider(monkeypatch):
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, AI_API_BASE_URL="https://ai.example.test", AI_API_KEY="key"),
        {
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
            "content-type": "application/json",
            "authorization": "Bearer client-token",
        },
        {"prompt": "hello"},
        url="https://api.test/v1/ai/chat/completions?stream=false",
    )

    class FakeResponse:
        status = 200
        headers = {"content-type": "application/json"}

        async def arrayBuffer(self):
            return b'{"choices": []}'

    calls = {}

    async def fake_fetch(url, **options):
        calls["url"] = url
        calls["options"] = options
        return FakeResponse()

    monkeypatch.setattr(entry, "worker_fetch", fake_fetch)
    response = asyncio.run(entry.ai_proxy(request, "chat/completions"))

    assert response.status_code == 200
    assert response.body == b'{"choices": []}'
    assert calls["url"] == "https://ai.example.test/chat/completions?stream=false"
    assert calls["options"]["headers"] == {"authorization": "Bearer key", "content-type": "application/json"}
    assert calls["options"]["body"] == b"audio"


def test_ai_proxy_fails_closed_when_provider_is_missing():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
        url="https://api.test/v1/ai/chat/completions",
    )

    response = asyncio.run(entry.ai_proxy(request, "chat/completions"))

    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "ai provider is not configured"}


def test_tts_validates_contract_and_returns_provider_audio(monkeypatch):
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(
            INTERNAL_ASSERTION_SECRET=secret,
            TTS_API_BASE_URL="https://tts.example.test/",
            TTS_API_KEY="key",
            TTS_MODEL="tts-model",
        ),
        {
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
        },
        {"text": " hello ", "voice_id": "sage", "instructions": "be warm"},
    )

    class FakeResponse:
        status = 200

        async def arrayBuffer(self):
            return b"ID3-mp3"

    calls = {}

    async def fake_fetch(url, **options):
        calls["url"] = url
        calls["options"] = options
        return FakeResponse()

    monkeypatch.setattr(entry, "worker_fetch", fake_fetch)
    response = asyncio.run(tts_synthesize(request))

    assert response.status_code == 200
    assert response.media_type == "audio/mpeg"
    assert response.body == b"ID3-mp3"
    assert calls["url"] == "https://tts.example.test/v1/audio/speech"
    assert json.loads(calls["options"]["body"]) == {
        "model": "tts-model",
        "input": "hello",
        "voice": "sage",
        "response_format": "mp3",
        "instructions": "be warm",
    }


def test_tts_rejects_unsupported_voice_before_provider_call():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, TTS_API_BASE_URL="https://tts.example.test", TTS_API_KEY="key"),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
        {"text": "hello", "voice_id": "not-a-voice"},
    )

    response = asyncio.run(tts_synthesize(request))

    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "voice_id is not supported"}
