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

from entry import (
    _provider_url,
    embeddings_workers_ai,
    transcribe,
    transcribe_workers_ai,
    translate_workers_ai,
    tts_synthesize,
    tts_synthesize_workers_ai,
    chat_messages,
)  # noqa: E402


class FakeRequest:
    def __init__(self, env, headers, json_body=None, *, method="POST", url="https://api.test/v1/tts/synthesize"):
        self.scope = {"env": env}
        self.headers = headers
        self.json_body = json_body or {"input": "hello"}
        self.method = method
        parsed_url = urlsplit(url)
        self.url = type("Url", (), {"path": parsed_url.path, "query": parsed_url.query})()
        self.query_params = {}

    async def body(self):
        return b"audio"

    async def json(self):
        return self.json_body


class FakeRateLimitStub:
    def __init__(self, owner):
        self.owner = owner

    async def checkTts(self, char_count):
        self.owner.calls.append((self.owner.current_name, char_count))
        if isinstance(self.owner.result, Exception):
            raise self.owner.result
        return self.owner.result

    async def health(self):
        self.owner.health_calls.append(self.owner.current_name)
        return {"status": "ok", "service": "rate-limit"}


class FakeRateLimits:
    def __init__(self, status=0):
        self.calls = []
        self.health_calls = []
        self.current_name = None
        self.result = {"status": status, "retryAfter": 60}

    def getByName(self, name):
        self.current_name = name
        return FakeRateLimitStub(self)


def signed_context(secret: str) -> tuple[str, str]:
    raw = json.dumps({"uid": "user-1"}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return encoded, base64.urlsafe_b64encode(signature).decode().rstrip("=")


def test_provider_url_normalizes_slashes():
    assert _provider_url("https://asr.example.test/", "/v2/transcribe") == "https://asr.example.test/v2/transcribe"


def test_health_checks_the_cross_worker_durable_object_binding():
    limiter = FakeRateLimits()
    response = asyncio.run(entry.health(FakeRequest(SimpleNamespace(RATE_LIMITS=limiter), {})))

    assert response == {"status": "ok", "service": "api-ai", "version": "cf-03"}
    assert limiter.health_calls == ["health:api-ai"]


def test_health_fails_closed_without_the_rate_limit_binding():
    response = asyncio.run(entry.health(FakeRequest(SimpleNamespace(), {})))

    assert response.status_code == 503
    assert json.loads(response.body) == {"status": "degraded", "dependency": "rate-limit"}


def test_chat_messages_fails_closed_without_workers_ai_binding():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
        {"text": "hello"},
        url="https://api.test/v2/messages",
    )

    response = asyncio.run(chat_messages(request))

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "error": "workers ai is not configured",
        "reason": "provider_not_configured",
    }


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
    assert calls == {
        "model": "@cf/openai/whisper",
        "payload": {"audio": base64.b64encode(b"audio").decode("ascii")},
    }


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


def test_workers_ai_translation_preserves_nllb_contract_and_maps_language_names():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    calls = []

    class FakeAI:
        async def run(self, model, payload):
            calls.append((model, payload))
            return {"translated_text": "你好"}

    request = FakeRequest(
        SimpleNamespace(
            INTERNAL_ASSERTION_SECRET=secret,
            AI=FakeAI(),
            WORKERS_AI_TRANSLATION_MODEL="@cf/meta/m2m100-1.2b",
        ),
        {
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
            "content-type": "application/json",
        },
        {
            "contents": ["hello"],
            "source_language_code": "en-US",
            "target_language_code": "zh-Hans",
        },
        url="https://api.test/v1/translate",
    )

    response = asyncio.run(translate_workers_ai(request))

    assert response["translations"] == [{"translated_text": "你好", "detected_language_code": "en"}]
    assert response["model"] == "@cf/meta/m2m100-1.2b"
    assert response["latency_ms"] >= 0
    assert calls == [
        (
            "@cf/meta/m2m100-1.2b",
            {"text": "hello", "source_lang": "english", "target_lang": "chinese"},
        )
    ]


def test_workers_ai_translation_rejects_unsupported_language():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, AI=object()),
        {
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
            "content-type": "application/json",
        },
        {"contents": ["hello"], "target_language_code": "ko"},
        url="https://api.test/v1/translate",
    )

    response = asyncio.run(translate_workers_ai(request))

    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "unsupported target language"}


def test_workers_ai_translation_fails_closed_without_binding():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret),
        {
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
            "content-type": "application/json",
        },
        {"contents": ["hello"], "target_language_code": "fr"},
        url="https://api.test/v1/translate",
    )

    response = asyncio.run(translate_workers_ai(request))

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


def test_workers_ai_embeddings_returns_openai_style_vectors():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    calls = {}

    class FakeAI:
        async def run(self, model, payload):
            calls["model"] = model
            calls["payload"] = payload
            return {"data": [[0.1, 0.2], [0.3, 0.4]]}

    request = FakeRequest(
        SimpleNamespace(
            INTERNAL_ASSERTION_SECRET=secret,
            AI=FakeAI(),
            WORKERS_AI_EMBEDDING_MODEL="@cf/baai/bge-base-en-v1.5",
        ),
        {
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
            "content-type": "application/json",
        },
        {"input": ["hello", "world"]},
        url="https://api.test/v1/embeddings-workers-ai",
    )

    response = asyncio.run(embeddings_workers_ai(request))

    assert response == {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": [0.1, 0.2], "index": 0},
            {"object": "embedding", "embedding": [0.3, 0.4], "index": 1},
        ],
        "model": "@cf/baai/bge-base-en-v1.5",
    }
    assert calls == {"model": "@cf/baai/bge-base-en-v1.5", "payload": {"text": ["hello", "world"]}}


def test_workers_ai_embeddings_rejects_oversized_input():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, AI=object()),
        {
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
            "content-type": "application/json",
        },
        {"input": "x" * 4_097},
        url="https://api.test/v1/embeddings-workers-ai",
    )

    response = asyncio.run(embeddings_workers_ai(request))

    assert response.status_code == 413
    assert json.loads(response.body) == {"error": "embedding input is too large or empty"}


def test_workers_ai_embeddings_fails_closed_without_binding():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret),
        {
            "x-omi-auth-context": encoded,
            "x-omi-internal-signature": signature,
            "content-type": "application/json",
        },
        {"input": "hello"},
        url="https://api.test/v1/embeddings-workers-ai",
    )

    response = asyncio.run(embeddings_workers_ai(request))

    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "workers ai is not configured"}


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
    limiter = FakeRateLimits()
    request = FakeRequest(
        SimpleNamespace(
            INTERNAL_ASSERTION_SECRET=secret,
            TTS_API_BASE_URL="https://tts.example.test/",
            TTS_API_KEY="key",
            TTS_MODEL="tts-model",
            RATE_LIMITS=limiter,
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
    assert limiter.calls == [("tts:fine:user-1", 5)]


def test_tts_rejects_unsupported_voice_before_provider_call():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(
            INTERNAL_ASSERTION_SECRET=secret, TTS_API_BASE_URL="https://tts.example.test", TTS_API_KEY="key"
        ),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
        {"text": "hello", "voice_id": "not-a-voice"},
    )

    response = asyncio.run(tts_synthesize(request))

    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "voice_id is not supported"}


def test_workers_ai_tts_uses_native_binding_and_returns_mp3():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    calls = {}

    class FakeResponse:
        async def bytes(self):
            return b"ID3-mp3"

    class FakeAI:
        async def run(self, model, payload, options):
            calls["model"] = model
            calls["payload"] = payload
            calls["options"] = options
            return FakeResponse()

    limiter = FakeRateLimits()
    request = FakeRequest(
        SimpleNamespace(
            INTERNAL_ASSERTION_SECRET=secret,
            AI=FakeAI(),
            WORKERS_AI_TTS_MODEL="@cf/deepgram/aura-1",
            RATE_LIMITS=limiter,
        ),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
        {"text": " hello ", "speaker": "Luna"},
        url="https://api.test/v1/tts/synthesize-workers-ai",
    )

    response = asyncio.run(tts_synthesize_workers_ai(request))

    assert response.status_code == 200
    assert response.media_type == "audio/mpeg"
    assert response.body == b"ID3-mp3"
    assert calls == {
        "model": "@cf/deepgram/aura-1",
        "payload": {"text": "hello", "speaker": "luna", "encoding": "mp3"},
        "options": {"returnRawResponse": True},
    }
    assert limiter.calls == [("tts:fine:user-1", 5)]


def test_workers_ai_tts_fails_closed_without_binding():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
        {"text": "hello"},
        url="https://api.test/v1/tts/synthesize-workers-ai",
    )

    response = asyncio.run(tts_synthesize_workers_ai(request))

    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "workers ai is not configured"}


def test_workers_ai_tts_rejects_unknown_speaker():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, AI=object()),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
        {"text": "hello", "speaker": "not-a-speaker"},
        url="https://api.test/v1/tts/synthesize-workers-ai",
    )

    response = asyncio.run(tts_synthesize_workers_ai(request))

    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "unsupported speaker"}


def test_workers_ai_tts_normalizes_model_failures_to_502():
    secret = "test-secret"
    encoded, signature = signed_context(secret)

    class FakeAI:
        async def run(self, model, payload, options):
            raise Exception("provider-specific FFI error")

    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, AI=FakeAI(), RATE_LIMITS=FakeRateLimits()),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
        {"text": "hello"},
        url="https://api.test/v1/tts/synthesize-workers-ai",
    )

    response = asyncio.run(tts_synthesize_workers_ai(request))

    assert response.status_code == 502
    assert json.loads(response.body) == {"error": "workers ai tts failed"}


def test_tts_fine_limit_rejects_burst_before_provider_call(monkeypatch):
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    limiter = FakeRateLimits(status=1)
    request = FakeRequest(
        SimpleNamespace(
            INTERNAL_ASSERTION_SECRET=secret,
            TTS_API_BASE_URL="https://tts.example.test",
            TTS_API_KEY="key",
            RATE_LIMITS=limiter,
        ),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
        {"text": "hello", "voice_id": "sage"},
    )

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(entry, "worker_fetch", unexpected_fetch)
    response = asyncio.run(tts_synthesize(request))

    assert response.status_code == 429
    assert json.loads(response.body) == {"detail": "TTS burst rate limit exceeded"}
    assert limiter.calls == [("tts:fine:user-1", 5)]


def test_workers_ai_tts_fine_limit_rejects_daily_budget_before_model_call():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    limiter = FakeRateLimits(status=2)

    class UnexpectedAI:
        async def run(self, *_args, **_kwargs):
            raise AssertionError("model must not be called")

    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, AI=UnexpectedAI(), RATE_LIMITS=limiter),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
        {"text": "hello", "speaker": "luna"},
        url="https://api.test/v1/tts/synthesize-workers-ai",
    )

    response = asyncio.run(tts_synthesize_workers_ai(request))

    assert response.status_code == 429
    assert json.loads(response.body) == {"detail": "TTS daily character limit exceeded"}


def test_tts_fine_limit_fails_closed_when_durable_object_is_unavailable():
    secret = "test-secret"
    encoded, signature = signed_context(secret)
    limiter = FakeRateLimits()
    limiter.result = RuntimeError("simulated DO outage")
    request = FakeRequest(
        SimpleNamespace(
            INTERNAL_ASSERTION_SECRET=secret,
            TTS_API_BASE_URL="https://tts.example.test",
            TTS_API_KEY="key",
            RATE_LIMITS=limiter,
        ),
        {"x-omi-auth-context": encoded, "x-omi-internal-signature": signature},
        {"text": "hello", "voice_id": "sage"},
    )

    response = asyncio.run(tts_synthesize(request))

    assert response.status_code == 503
    assert json.loads(response.body) == {"detail": "TTS rate limiting is unavailable"}
