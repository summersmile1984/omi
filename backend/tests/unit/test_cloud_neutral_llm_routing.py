import pytest

from utils.llm import model_config


def test_translation_override_reads_environment_at_call_boundary(monkeypatch):
    monkeypatch.setenv('TRANSLATION_PROVIDER', 'deepseek')
    monkeypatch.delenv('TRANSLATION_MODEL', raising=False)
    assert model_config.get_model_config('translation') == ('deepseek-chat', 'deepseek')

    monkeypatch.setenv('TRANSLATION_PROVIDER', 'mimo')
    monkeypatch.setenv('TRANSLATION_MODEL', 'mimo-custom')
    assert model_config.get_model_config('translation') == ('mimo-custom', 'mimo')


def test_chat_override_applies_only_to_supported_chat_features(monkeypatch):
    monkeypatch.setenv('CHAT_PROVIDER', 'deepseek')
    monkeypatch.delenv('CHAT_MODEL', raising=False)

    assert model_config.get_model_config('chat_responses') == ('deepseek-v4-flash', 'deepseek')
    assert model_config.get_model_config('chat_extraction') == ('deepseek-v4-flash', 'deepseek')
    assert model_config.get_model_config('chat_graph') == ('deepseek-v4-flash', 'deepseek')
    assert model_config.get_model_config('chat_agent') == ('claude-sonnet-4-6', 'anthropic')


def test_unknown_override_preserves_upstream_profile(monkeypatch):
    monkeypatch.setenv('TRANSLATION_PROVIDER', 'unknown')
    monkeypatch.setenv('CHAT_PROVIDER', 'unknown')

    assert model_config.get_model_config('translation') == ('gemini-2.5-flash-lite', 'gemini')
    assert model_config.get_model_config('chat_responses') == ('gpt-5.6-luna', 'openai')


def test_neutral_deployment_cannot_fall_back_to_checked_in_vendor_profile(monkeypatch):
    for name in (
        'OMI_LLM_ROUTE_CHAT_RESPONSES_PROVIDER',
        'OMI_LLM_ROUTE_CHAT_RESPONSES_MODEL',
        'OMI_LLM_DEFAULT_PROVIDER',
        'OMI_LLM_DEFAULT_MODEL',
        'CHAT_PROVIDER',
        'CHAT_MODEL',
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')

    with pytest.raises(ValueError, match='explicit provider/model route'):
        model_config.get_model_config('chat_responses')


def test_neutral_deployment_accepts_explicit_operator_route(monkeypatch):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.setenv('OMI_LLM_DEFAULT_PROVIDER', 'generic')
    monkeypatch.setenv('OMI_LLM_DEFAULT_MODEL', 'local-chat')

    assert model_config.get_model_config('chat_responses') == ('local-chat', 'generic')


def test_neutral_deployment_provider_only_route_does_not_use_profile_model_or_ambient_key(monkeypatch):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.setenv('OMI_LLM_DEFAULT_PROVIDER', 'openai')
    monkeypatch.delenv('OMI_LLM_DEFAULT_MODEL', raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'ambient-must-not-change-routing')

    with pytest.raises(ValueError, match='explicit provider/model route'):
        model_config.get_model_config('chat_responses')


def test_neutral_deployment_group_provider_only_route_requires_model(monkeypatch):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'neutral')
    monkeypatch.setenv('CHAT_PROVIDER', 'deepseek')
    monkeypatch.delenv('CHAT_MODEL', raising=False)
    monkeypatch.delenv('DEEPSEEK_MODEL', raising=False)

    with pytest.raises(ValueError, match='explicit provider/model route'):
        model_config.get_model_config('chat_responses')
