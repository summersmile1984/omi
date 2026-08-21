from __future__ import annotations

from typing import Any

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from utils.llm.direct_fallback import BoundedFallbackChatModel
from utils.llm import clients, providers
from utils.llm.providers import ModelProviderConfigurationError


class SequencedChatModel(BaseChatModel):
    model_name: str
    outcomes: list[Any]
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return f'fake-{self.model_name}'

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=str(outcome)))])

    def _stream(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        yield ChatGenerationChunk(message=AIMessageChunk(content=str(outcome)))

    def with_structured_output(self, _schema, *, include_raw=False, **_kwargs):
        return self

    def bind_tools(self, _tools, *, tool_choice=None, **_kwargs):
        return self


def _fallback_model(primary: SequencedChatModel, fallback: SequencedChatModel) -> BoundedFallbackChatModel:
    return BoundedFallbackChatModel(
        primary=primary,
        fallback_models=(fallback,),
        route_labels=('generic:primary', 'deepseek:fallback'),
        feature='conv_structure',
    )


def test_direct_mode_executes_the_manifest_fallback_for_a_pre_output_timeout():
    primary = SequencedChatModel(model_name='primary', outcomes=[httpx.ReadTimeout('timed out')])
    fallback = SequencedChatModel(model_name='fallback', outcomes=['recovered'])

    result = _fallback_model(primary, fallback).invoke('hello')

    assert result.content == 'recovered'
    assert primary.calls == 1
    assert fallback.calls == 1


def test_direct_mode_does_not_fallback_for_validation_or_application_errors():
    primary = SequencedChatModel(model_name='primary', outcomes=[ValueError('invalid request')])
    fallback = SequencedChatModel(model_name='fallback', outcomes=['must not run'])

    with pytest.raises(ValueError, match='invalid request'):
        _fallback_model(primary, fallback).invoke('hello')

    assert primary.calls == 1
    assert fallback.calls == 0


def test_direct_mode_preserves_bounded_fallback_after_structured_output_binding():
    primary = SequencedChatModel(model_name='primary', outcomes=[httpx.ReadTimeout('timed out')])
    fallback = SequencedChatModel(model_name='fallback', outcomes=['structured recovery'])

    result = _fallback_model(primary, fallback).with_structured_output({'type': 'object'}).invoke('hello')

    assert result.content == 'structured recovery'
    assert primary.calls == 1
    assert fallback.calls == 1


def test_direct_mode_preserves_bounded_fallback_after_tool_binding():
    primary = SequencedChatModel(model_name='primary', outcomes=[httpx.ReadTimeout('timed out')])
    fallback = SequencedChatModel(model_name='fallback', outcomes=['tool recovery'])

    result = (
        _fallback_model(primary, fallback)
        .bind_tools(
            [{'type': 'function', 'function': {'name': 'capture', 'parameters': {'type': 'object'}}}],
            tool_choice='auto',
        )
        .invoke('hello')
    )

    assert result.content == 'tool recovery'
    assert primary.calls == 1
    assert fallback.calls == 1


def test_direct_mode_preserves_bounded_fallback_before_stream_output():
    primary = SequencedChatModel(model_name='primary', outcomes=[httpx.ReadTimeout('timed out')])
    fallback = SequencedChatModel(model_name='fallback', outcomes=['stream recovery'])

    chunks = list(_fallback_model(primary, fallback).stream('hello'))

    assert ''.join(str(chunk.content) for chunk in chunks) == 'stream recovery'
    assert primary.calls == 1
    assert fallback.calls == 1


@pytest.mark.parametrize('status_code', [429, 500, 503])
def test_direct_mode_uses_the_gateway_eligible_provider_failure_classes(status_code):
    request = httpx.Request('POST', 'http://provider.internal/v1/chat/completions')
    response = httpx.Response(status_code, request=request)
    primary = SequencedChatModel(
        model_name='primary',
        outcomes=[httpx.HTTPStatusError('provider failure', request=request, response=response)],
    )
    fallback = SequencedChatModel(model_name='fallback', outcomes=['recovered'])

    assert _fallback_model(primary, fallback).invoke('hello').content == 'recovered'


def test_get_llm_builds_direct_fallbacks_from_the_shared_feature_manifest(monkeypatch):
    monkeypatch.setenv('OMI_LLM_ROUTE_CONV_STRUCTURE_PROVIDER', 'generic')
    monkeypatch.setenv('OMI_LLM_ROUTE_CONV_STRUCTURE_MODEL', 'primary-model')
    monkeypatch.setenv('OMI_LLM_ROUTE_CONV_STRUCTURE_FALLBACKS', 'deepseek:fallback-model')
    monkeypatch.setattr(clients, 'should_route_features_through_gateway', lambda: False)
    monkeypatch.setattr(clients, 'get_byok_key', lambda _provider: None)
    monkeypatch.setattr(
        clients,
        'maybe_wrap_dev_gateway_shadow',
        lambda *, legacy_model, **_kwargs: legacy_model,
    )
    models = {
        ('generic', 'primary-model'): SequencedChatModel(
            model_name='primary', outcomes=[httpx.ReadTimeout('timed out')]
        ),
        ('deepseek', 'fallback-model'): SequencedChatModel(model_name='fallback', outcomes=['manifest recovered']),
    }
    monkeypatch.setattr(
        clients,
        'get_default_client',
        lambda model, provider, _streaming, _options: models[(provider, model)],
    )

    result = clients.get_llm('conv_structure')

    assert isinstance(result, BoundedFallbackChatModel)
    assert result.invoke('hello').content == 'manifest recovered'


def test_get_llm_filters_an_unconfigured_optional_fallback_without_blocking_the_primary(monkeypatch):
    monkeypatch.setenv('OMI_LLM_ROUTE_CONV_STRUCTURE_PROVIDER', 'generic')
    monkeypatch.setenv('OMI_LLM_ROUTE_CONV_STRUCTURE_MODEL', 'primary-model')
    monkeypatch.setenv('OMI_LLM_ROUTE_CONV_STRUCTURE_FALLBACKS', 'deepseek:fallback-model')
    monkeypatch.setenv('GENERIC_OPENAI_BASE_URL', 'http://127.0.0.1:11434/v1')
    monkeypatch.setenv('GENERIC_OPENAI_API_KEY', 'local-key')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setattr(clients, 'should_route_features_through_gateway', lambda: False)
    monkeypatch.setattr(clients, 'get_byok_key', lambda _provider: None)
    monkeypatch.setattr(
        clients,
        'maybe_wrap_dev_gateway_shadow',
        lambda *, legacy_model, **_kwargs: legacy_model,
    )
    primary = SequencedChatModel(model_name='primary', outcomes=['primary answer'])
    constructor_calls: list[dict[str, Any]] = []
    providers._llm_cache.clear()

    def configured_client(**kwargs):
        constructor_calls.append(kwargs)
        return primary

    monkeypatch.setattr(providers, 'ChatOpenAI', configured_client)

    result = clients.get_llm('conv_structure')

    assert result is primary
    assert result.invoke('hello').content == 'primary answer'
    assert len(constructor_calls) == 1
    assert constructor_calls[0]['model'] == 'primary-model'
    assert constructor_calls[0]['base_url'] == 'http://127.0.0.1:11434/v1'


def test_get_llm_fails_before_constructing_a_keyless_primary(monkeypatch):
    monkeypatch.setenv('OMI_LLM_ROUTE_CONV_STRUCTURE_PROVIDER', 'generic')
    monkeypatch.setenv('OMI_LLM_ROUTE_CONV_STRUCTURE_MODEL', 'primary-model')
    monkeypatch.delenv('OMI_LLM_ROUTE_CONV_STRUCTURE_FALLBACKS', raising=False)
    monkeypatch.setenv('GENERIC_OPENAI_BASE_URL', 'http://127.0.0.1:11434/v1')
    monkeypatch.delenv('GENERIC_OPENAI_API_KEY', raising=False)
    monkeypatch.setattr(clients, 'should_route_features_through_gateway', lambda: False)
    monkeypatch.setattr(clients, 'get_byok_key', lambda _provider: None)
    monkeypatch.setattr(
        'utils.llm.providers.ChatOpenAI',
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError('provider client must not be constructed')),
    )

    with pytest.raises(ModelProviderConfigurationError) as error:
        clients.get_llm('conv_structure')

    assert error.value.provider == 'generic'
    assert error.value.reason == 'credential_not_configured'


def test_legacy_mini_alias_resolves_through_the_feature_manifest(monkeypatch):
    selected = SequencedChatModel(model_name='selected', outcomes=['answer'])
    calls = []
    monkeypatch.setattr(clients, 'get_llm', lambda feature, **kwargs: calls.append((feature, kwargs)) or selected)

    assert clients._create_legacy_llm_mini() is selected
    assert calls == [('learnings', {'request_timeout': 120, 'max_retries': 1})]
