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
