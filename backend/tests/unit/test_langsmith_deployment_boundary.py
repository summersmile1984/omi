"""Deployment-profile guards for optional LangSmith observability."""

import os

from utils.observability import langsmith
from utils.observability import langsmith_prompts


def _clear_langsmith_env(monkeypatch):
    for name in (
        "LANGSMITH_API_KEY",
        "LANGCHAIN_API_KEY",
        "LANGSMITH_ENDPOINT",
        "LANGCHAIN_ENDPOINT",
        "LANGSMITH_TRACING",
        "LANGSMITH_TRACING_V2",
        "LANGCHAIN_TRACING",
        "LANGCHAIN_TRACING_V2",
    ):
        monkeypatch.delenv(name, raising=False)


def test_neutral_profile_ignores_ambient_langsmith_credentials(monkeypatch):
    _clear_langsmith_env(monkeypatch)
    monkeypatch.setenv("OMI_DEPLOYMENT_PROFILE", "self_hosted")
    monkeypatch.setenv("LANGSMITH_API_KEY", "ambient-managed-secret")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")

    assert langsmith.is_langsmith_enabled() is False
    assert langsmith.has_langsmith_api_key() is False
    assert langsmith.get_langsmith_endpoint() == ""
    assert langsmith.get_chat_tracer_callbacks(run_name="neutral") == []
    assert langsmith.submit_langsmith_feedback("run-id", 1.0) is False
    assert langsmith_prompts._fetch_prompt_from_langsmith("ambient-prompt") is None
    assert all(os.environ[name] == "false" for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"))


def test_managed_profile_preserves_explicit_langsmith_behavior(monkeypatch):
    _clear_langsmith_env(monkeypatch)
    monkeypatch.setenv("OMI_DEPLOYMENT_PROFILE", "omi_cloud")
    monkeypatch.setenv("LANGSMITH_API_KEY", "managed-key")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://operator-observability.example")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    assert langsmith.is_langsmith_enabled() is True
    assert langsmith.has_langsmith_api_key() is True
    assert langsmith.get_langsmith_endpoint() == "https://operator-observability.example"
