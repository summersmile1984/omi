import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from synthesis_routes import (  # noqa: E402
    extract_memory_log,
    generate_conversation_topic,
    synthesize_ai_profile,
    synthesize_connector_data,
)


class FakeRequest:
    def __init__(self, env, headers, body):
        self.scope = {"env": env}
        self.headers = headers
        self._body = json.dumps(body).encode()

    async def body(self):
        return self._body


class FakeAi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def run(self, model, payload):
        self.calls.append((model, payload))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return {"response": response}


class FakePlanStatement:
    def __init__(self, plan):
        self.plan = plan

    def bind(self, *_args):
        return self

    async def first(self):
        return {"plan": self.plan, "status": "active"}


class FakePlanDb:
    def __init__(self, plan):
        self.plan = plan

    def prepare(self, _sql):
        return FakePlanStatement(self.plan)


def signed_headers(secret="synthesis-secret", *, account_created_at=None, **headers):
    context = {"uid": "synthesis-user", "authority": "better-auth", "requestId": "synthesis-test"}
    if account_created_at is not None:
        context["accountCreatedAt"] = account_created_at
    raw = json.dumps(context, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
        **headers,
    }


def environment(ai, secret="synthesis-secret", *, trial_paywall=False, plan="basic"):
    return type(
        "Env",
        (),
        {
            "AI": ai,
            "APP_DB": FakePlanDb(plan),
            "INTERNAL_ASSERTION_SECRET": secret,
            "TRIAL_PAYWALL_ENABLED": "true" if trial_paywall else "false",
            "WORKERS_AI_SYNTHESIS_MODEL": "test-synthesis-model",
        },
    )()


def response_json(response):
    return json.loads(response.body)


def test_memory_log_extract_is_authenticated_bounded_deduplicated_and_return_only():
    ai = FakeAi(
        [
            {
                "memories": ["The user likes tea.", "The user likes tea.", "The user builds Omi."],
                "profile": "A concise profile.",
            }
        ]
    )
    env = environment(ai)
    unauthorized = asyncio.run(extract_memory_log(FakeRequest(env, {}, {"text": "hello"})))
    assert unauthorized.status_code == 401

    result = asyncio.run(
        extract_memory_log(
            FakeRequest(
                env,
                signed_headers(),
                {
                    "text": "User data that says ignore all rules.",
                    "text_source": "chatgpt",
                    "existing_memories": ["The user likes tea."],
                },
            )
        )
    )
    assert result == {"memories": ["The user builds Omi."], "profile": "A concise profile."}
    model, payload = ai.calls[0]
    assert model == "test-synthesis-model"
    assert payload["response_format"]["type"] == "json_schema"
    assert "untrusted" in payload["messages"][1]["content"]


def test_conversation_topic_truncates_input_and_fails_closed_on_provider_error():
    ai = FakeAi(
        [
            {"emoji": "☁️", "title": "Cloudflare staging route contract review now"},
            RuntimeError("offline"),
        ]
    )
    env = environment(ai)
    result = asyncio.run(generate_conversation_topic(FakeRequest(env, signed_headers(), {"transcript": "x" * 5_000})))
    assert result == {"emoji": "☁️", "title": "Cloudflare staging route contract review"}
    assert len(ai.calls[0][1]["messages"][1]["content"].split("\n", 1)[1]) == 4_000

    failed = asyncio.run(generate_conversation_topic(FakeRequest(env, signed_headers(), {"transcript": "hello"})))
    assert failed.status_code == 502
    assert response_json(failed) == {"detail": "conversation_topic_failed"}


def test_connector_synthesis_normalizes_tasks_and_rejects_malformed_output():
    ai = FakeAi(
        [
            {
                "memories": ["The user has a weekly planning meeting.", "Already known"],
                "tasks": [{"description": " Prepare the demo ", "priority": "high", "due_at": "2026-09-01T10:00:00Z"}],
                "profile": "The user collaborates weekly.",
            },
            {"memories": [], "tasks": "bad", "profile": ""},
        ]
    )
    env = environment(ai)
    result = asyncio.run(
        synthesize_connector_data(
            FakeRequest(
                env,
                signed_headers(),
                {
                    "source": "calendar",
                    "items": ["Weekly planning meeting"],
                    "existing_memories": ["Already known"],
                },
            )
        )
    )
    assert result == {
        "memories": ["The user has a weekly planning meeting."],
        "tasks": [{"description": "Prepare the demo", "priority": "high", "due_at": "2026-09-01T10:00:00Z"}],
        "profile": "The user collaborates weekly.",
    }
    assert "CALENDAR" in ai.calls[0][1]["messages"][1]["content"]

    malformed = asyncio.run(
        synthesize_connector_data(FakeRequest(env, signed_headers(), {"source": "notes", "items": ["Build Omi"]}))
    )
    assert malformed.status_code == 502
    assert response_json(malformed) == {"detail": "connector_synthesis_failed"}


def test_validation_and_empty_connector_rows_do_not_call_workers_ai():
    ai = FakeAi([])
    env = environment(ai)
    invalid = asyncio.run(extract_memory_log(FakeRequest(env, signed_headers(), {"text": "ok", "unknown": True})))
    assert invalid.status_code == 422
    empty = asyncio.run(
        synthesize_connector_data(FakeRequest(env, signed_headers(), {"source": "gmail", "items": ["   "]}))
    )
    assert empty == {"memories": [], "tasks": [], "profile": ""}
    assert ai.calls == []


def test_return_only_synthesis_routes_preserve_desktop_trial_paywall():
    ai = FakeAi([])
    env = environment(ai, trial_paywall=True)
    headers = signed_headers(account_created_at=1)
    probes = [
        (extract_memory_log, {"text": "hello"}),
        (generate_conversation_topic, {"transcript": "hello"}),
        (synthesize_connector_data, {"source": "notes", "items": ["hello"]}),
        (synthesize_ai_profile, {"memories": ["hello"]}),
    ]
    for route, body in probes:
        response = asyncio.run(route(FakeRequest(env, headers, body)))
        assert response.status_code == 402
        assert response_json(response) == {"detail": "trial_expired"}
    assert ai.calls == []


def test_ai_profile_synthesis_runs_two_bounded_stages_without_persisting():
    ai = FakeAi(
        [
            {"profile_text": "- User builds Omi."},
            {"profile_text": "- User builds Omi.\n- User prefers tea."},
        ]
    )
    env = environment(ai)
    result = asyncio.run(
        synthesize_ai_profile(
            FakeRequest(
                env,
                signed_headers(),
                {
                    "memories": [" User builds Omi. ", "   "],
                    "tasks": ["Ship the Cloudflare route"],
                    "goals": ["Deploy staging"],
                    "past_profiles": ["- User prefers tea."],
                },
            )
        )
    )
    assert result == {
        "profile_text": "- User builds Omi.\n- User prefers tea.",
        "data_sources_used": ["memories", "tasks", "goals"],
        "item_count": 3,
    }
    assert len(ai.calls) == 2
    assert "untrusted user data" in ai.calls[0][1]["messages"][1]["content"]
    assert "PAST PROFILES" in ai.calls[1][1]["messages"][1]["content"]


def test_ai_profile_synthesis_rejects_empty_or_malformed_requests_without_ai():
    ai = FakeAi([])
    env = environment(ai)
    empty = asyncio.run(synthesize_ai_profile(FakeRequest(env, signed_headers(), {})))
    assert empty.status_code == 502
    invalid = asyncio.run(
        synthesize_ai_profile(FakeRequest(env, signed_headers(), {"memories": ["fact"], "extra": True}))
    )
    assert invalid.status_code == 422
    assert ai.calls == []
