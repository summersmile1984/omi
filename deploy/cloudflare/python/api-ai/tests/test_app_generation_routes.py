import asyncio
import base64
import hashlib
import hmac
import json
from types import SimpleNamespace

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from app_generation_routes import (
    GenerateDescriptionEmojiRequest,
    GenerateDescriptionRequest,
    generate_description,
    generate_description_and_emoji,
    generate_sample_prompts,
)


def _signed_headers(secret: str) -> dict[str, str]:
    raw = json.dumps({"uid": "user-1"}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


class FakeRequest:
    def __init__(self, env, headers=None):
        self.scope = {"env": env}
        self.headers = headers or {}


class FakeD1:
    def __init__(self):
        self.bindings = []

    def prepare(self, _sql):
        return self

    def bind(self, *values):
        self.bindings.append(values)
        return self

    async def run(self):
        return None


def test_generate_prompts_requires_authenticated_context():
    response = asyncio.run(generate_sample_prompts(FakeRequest(SimpleNamespace(INTERNAL_ASSERTION_SECRET="secret"))))

    assert response.status_code == 401
    assert json.loads(response.body) == {"error": "unauthorized"}


def test_generate_prompts_uses_workers_ai_and_records_usage():
    calls = {}

    class FakeAI:
        async def run(self, model, payload):
            calls["model"] = model
            calls["payload"] = payload
            return {
                "response": '["Conversation map", "Funny moments", "Commitment tracker", "Startup advisor", "Focus coach"]',
                "usage": {"prompt_tokens": 9, "completion_tokens": 8},
            }

    database = FakeD1()
    secret = "secret"
    response = asyncio.run(
        generate_sample_prompts(
            FakeRequest(
                SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, AI=FakeAI(), APP_DB=database),
                _signed_headers(secret),
            )
        )
    )

    assert response == {
        "prompts": ["Conversation map", "Funny moments", "Commitment tracker", "Startup advisor", "Focus coach"]
    }
    assert calls["model"] == "@cf/meta/llama-3.2-3b-instruct"
    assert calls["payload"]["max_tokens"] == 192
    assert database.bindings[0][0] == "user-1"
    assert database.bindings[0][2:6] == ("@cf/meta/llama-3.2-3b-instruct", 9, 8, 17)


def test_generate_prompts_accepts_fenced_json_and_falls_back_for_invalid_output():
    class FakeAI:
        def __init__(self, response):
            self.response = response

        async def run(self, _model, _payload):
            return {"response": self.response}

    secret = "secret"
    fenced = "```json\n[\"one\", \"two\", \"three\", \"four\", \"five\"]\n```"
    generated = asyncio.run(
        generate_sample_prompts(
            FakeRequest(SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, AI=FakeAI(fenced)), _signed_headers(secret))
        )
    )
    invalid = asyncio.run(
        generate_sample_prompts(
            FakeRequest(
                SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, AI=FakeAI("not json")), _signed_headers(secret)
            )
        )
    )

    assert generated["prompts"] == ["one", "two", "three", "four", "five"]
    assert invalid["prompts"] == [
        "Mind map generator from conversations",
        "Jokes and funny moments extractor",
        "Key decisions and commitments tracker",
        "Elon Musk startup advisor clone",
        "Strict accountability coach",
    ]


def test_generate_prompts_preserves_static_fallback_without_workers_ai():
    secret = "secret"
    response = asyncio.run(
        generate_sample_prompts(FakeRequest(SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret), _signed_headers(secret)))
    )

    assert response["prompts"][0] == "Mind map generator from conversations"
    assert len(response["prompts"]) == 5


def test_generate_description_uses_workers_ai_and_validates_required_fields():
    class FakeAI:
        async def run(self, _model, _payload):
            return {
                "response": "A concise, polished description",
                "usage": {"prompt_tokens": 5, "completion_tokens": 6},
            }

    secret = "secret"
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, AI=FakeAI(), APP_DB=FakeD1()),
        _signed_headers(secret),
    )
    response = asyncio.run(
        generate_description(request, GenerateDescriptionRequest(name="Focus", description="Tracks focus"))
    )
    missing_name = asyncio.run(
        generate_description(request, GenerateDescriptionRequest(name=" ", description="Tracks focus"))
    )

    assert response == {"description": "A concise, polished description"}
    assert missing_name.status_code == 422
    assert json.loads(missing_name.body) == {"detail": "App Name is required"}


def test_generate_description_emoji_parses_json_and_keeps_legacy_fallback():
    class FakeAI:
        def __init__(self, response):
            self.response = response

        async def run(self, _model, _payload):
            return {"response": self.response}

    secret = "secret"
    request = FakeRequest(
        SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret, AI=FakeAI('{"description":"A focus helper","emoji":"🎯"}')),
        _signed_headers(secret),
    )
    generated = asyncio.run(
        generate_description_and_emoji(request, GenerateDescriptionEmojiRequest(name="Focus", prompt="tracks focus"))
    )
    request.scope["env"].AI = FakeAI("not json")
    fallback = asyncio.run(
        generate_description_and_emoji(request, GenerateDescriptionEmojiRequest(name="Focus", prompt="tracks focus"))
    )

    assert generated == {"description": "A focus helper", "emoji": "🎯"}
    assert fallback == {"description": "A custom app that tracks focus", "emoji": "✨"}


def test_generate_description_fails_closed_when_workers_ai_is_unavailable():
    secret = "secret"
    response = asyncio.run(
        generate_description(
            FakeRequest(SimpleNamespace(INTERNAL_ASSERTION_SECRET=secret), _signed_headers(secret)),
            GenerateDescriptionRequest(name="Focus", description="Tracks focus"),
        )
    )

    assert response.status_code == 502
    assert json.loads(response.body) == {"error": "app description generation unavailable"}
