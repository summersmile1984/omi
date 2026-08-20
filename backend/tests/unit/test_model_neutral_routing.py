from __future__ import annotations

import json

import pytest

from llm_gateway.gateway.auth import ServiceCaller
from llm_gateway.gateway.config_loader import feature_lane_id, load_gateway_config
from llm_gateway.gateway.credentials import build_omi_managed_credential_context
from llm_gateway.gateway.executor import ProviderRegistry, execute_chat_completion
from llm_gateway.gateway.providers import FakeChatCompletionProvider, ProviderFailure, fake_success_response
from llm_gateway.gateway.resolver import resolve_chat_completion_route
from llm_gateway.gateway.schemas import FailureClass
from llm_gateway.routers import dependencies
from utils.llm import clients
from utils.llm import providers
from utils.llm.model_config import (
    DEFAULT_ROUTE_MODEL_ENV,
    DEFAULT_ROUTE_PROVIDER_ENV,
    get_all_configured_features,
    get_feature_route_manifest,
    resolve_feature_route,
)
from utils.retrieval.tools.perplexity_tools import perplexity_web_search_tool

OFFICIAL_MODEL_DOMAINS = (
    'api.openai.com',
    'api.deepseek.com',
    'xiaomimimo.com',
    'openrouter.ai',
    'generativelanguage.googleapis.com',
)


def test_every_configured_feature_has_a_unique_per_feature_env_contract():
    manifest = get_feature_route_manifest()

    assert set(manifest) == get_all_configured_features()
    assert len(manifest) == 46  # 45 profile workloads plus the pinned fair-use classifier.
    assert len({spec.provider_env for spec in manifest.values()}) == len(manifest)
    assert len({spec.model_env for spec in manifest.values()}) == len(manifest)
    assert len({spec.fallbacks_env for spec in manifest.values()}) == len(manifest)


def test_per_feature_generic_route_matrix_covers_the_complete_manifest():
    for feature, spec in get_feature_route_manifest().items():
        route = resolve_feature_route(
            feature,
            {
                spec.provider_env: 'generic',
                spec.model_env: f'local-{feature}',
                spec.fallbacks_env: 'deepseek:deepseek-chat,mimo:mimo-v2.5',
            },
        )

        assert route.primary.provider == 'generic'
        assert route.primary.model == f'local-{feature}'
        assert [(fallback.provider, fallback.model) for fallback in route.fallbacks] == [
            ('deepseek', 'deepseek-chat'),
            ('mimo', 'mimo-v2.5'),
        ]
        assert route.source == 'feature_env'


def test_per_feature_route_beats_group_and_deployment_defaults():
    spec = get_feature_route_manifest()['translation']
    route = resolve_feature_route(
        'translation',
        {
            DEFAULT_ROUTE_PROVIDER_ENV: 'mimo',
            DEFAULT_ROUTE_MODEL_ENV: 'global-model',
            'TRANSLATION_PROVIDER': 'deepseek',
            'TRANSLATION_MODEL': 'group-model',
            spec.provider_env: 'generic',
            spec.model_env: 'feature-model',
        },
    )

    assert route.primary.provider == 'generic'
    assert route.primary.model == 'feature-model'
    assert route.source == 'feature_env'


def test_gateway_uses_the_same_global_generic_route_and_executable_fallbacks(monkeypatch):
    monkeypatch.setenv(DEFAULT_ROUTE_PROVIDER_ENV, 'generic')
    monkeypatch.setenv(DEFAULT_ROUTE_MODEL_ENV, 'local-chat')
    monkeypatch.setenv('OMI_LLM_DEFAULT_FALLBACKS', 'deepseek:deepseek-chat,mimo:mimo-v2.5')

    config = load_gateway_config(prod_mode=True)
    for feature in get_all_configured_features():
        direct = resolve_feature_route(feature)
        lane = config.lanes[feature_lane_id(feature)]
        route = config.route_artifacts[lane.active_route]
        assert (route.primary.provider, route.primary.model) == (direct.primary.provider, direct.primary.model)
        assert [(fallback.provider, fallback.model) for fallback in route.fallbacks] == [
            (fallback.provider, fallback.model) for fallback in direct.fallbacks
        ]
        assert route.primary.provider == 'generic'


def test_direct_client_selection_uses_the_global_generic_route_for_every_feature(monkeypatch):
    monkeypatch.setenv(DEFAULT_ROUTE_PROVIDER_ENV, 'generic')
    monkeypatch.setenv(DEFAULT_ROUTE_MODEL_ENV, 'local-chat')
    monkeypatch.delenv('CHAT_PROVIDER', raising=False)
    monkeypatch.delenv('TRANSLATION_PROVIDER', raising=False)
    monkeypatch.setattr(clients, 'should_route_features_through_gateway', lambda: False)
    monkeypatch.setattr(clients, 'get_byok_key', lambda _provider: None)
    monkeypatch.setattr(
        clients,
        'maybe_wrap_dev_gateway_shadow',
        lambda *, legacy_model, **_kwargs: legacy_model,
    )
    selected = []

    def fake_default_client(model, provider, streaming, options):
        selected.append((model, provider, streaming, options))
        return (provider, model)

    monkeypatch.setattr(clients, 'get_default_client', fake_default_client)

    for feature in sorted(get_all_configured_features()):
        assert clients.get_llm(feature) == ('generic', 'local-chat')

    assert selected == [('local-chat', 'generic', False, {})] * len(get_all_configured_features())


@pytest.mark.asyncio
async def test_manifest_fallback_is_executed_after_a_generic_provider_failure(monkeypatch):
    monkeypatch.setenv(DEFAULT_ROUTE_PROVIDER_ENV, 'generic')
    monkeypatch.setenv(DEFAULT_ROUTE_MODEL_ENV, 'local-chat')
    monkeypatch.setenv('OMI_LLM_DEFAULT_FALLBACKS', 'deepseek:deepseek-chat')
    config = load_gateway_config(prod_mode=True)
    resolved = resolve_chat_completion_route(
        config,
        {
            'model': 'omi:auto:conv-structure',
            'messages': [{'role': 'user', 'content': 'Summarize this conversation.'}],
        },
    )
    generic = FakeChatCompletionProvider([ProviderFailure(FailureClass.TIMEOUT_BEFORE_OUTPUT)])
    deepseek_ref = resolved.active_route.fallbacks[0]
    deepseek = FakeChatCompletionProvider([fake_success_response(deepseek_ref, content='fallback result')])

    result = await execute_chat_completion(
        resolved,
        build_omi_managed_credential_context(ServiceCaller(name='backend')),
        ProviderRegistry({'generic': generic, 'deepseek': deepseek}),
    )

    assert result.selected_provider == 'deepseek'
    assert result.selected_model == 'deepseek-chat'
    assert result.fallback_used is True
    assert result.fallback_reason == FailureClass.TIMEOUT_BEFORE_OUTPUT
    assert [call.model for call in generic.calls] == ['local-chat']
    assert [call.model for call in deepseek.calls] == ['deepseek-chat']


@pytest.mark.asyncio
async def test_disabled_web_search_never_calls_the_omi_gateway(monkeypatch):
    monkeypatch.setenv('WEB_SEARCH_TRANSPORT', 'disabled')

    async def forbidden_gateway_call(_query):
        raise AssertionError('disabled self-host web search must not use the managed gateway')

    monkeypatch.setattr(
        'utils.retrieval.tools.perplexity_tools._perplexity_gateway_search',
        forbidden_gateway_call,
    )

    result = await perplexity_web_search_tool.ainvoke({'query': 'latest news'})

    assert json.loads(result) == {
        'code': 'model_capability_unavailable',
        'capability': 'web_search',
        'reason': 'disabled_by_deployment',
        'retryable': False,
    }


def test_generic_direct_client_requires_an_explicit_non_vendor_endpoint(monkeypatch):
    captured = {}
    for env_name in (
        'OPENAI_API_KEY',
        'DEEPSEEK_API_KEY',
        'MIMO_API_KEY',
        'OPENROUTER_API_KEY',
        'GEMINI_API_KEY',
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv('GENERIC_OPENAI_BASE_URL', 'http://127.0.0.1:11434/v1')
    monkeypatch.setenv('GENERIC_OPENAI_API_KEY', 'local-only')
    providers._llm_cache.clear()

    def fake_chat_openai(*, model, **kwargs):
        captured.update(model=model, **kwargs)
        return object()

    monkeypatch.setattr(providers, 'ChatOpenAI', fake_chat_openai)
    providers.get_or_create_openai_compatible_llm('generic', 'local-model')

    assert captured['base_url'] == 'http://127.0.0.1:11434/v1'
    assert captured['api_key'] == 'local-only'
    assert not any(domain in captured['base_url'] for domain in OFFICIAL_MODEL_DOMAINS)
    providers._llm_cache.clear()


def test_generic_direct_client_fails_closed_without_a_base_url(monkeypatch):
    monkeypatch.delenv('GENERIC_OPENAI_BASE_URL', raising=False)
    monkeypatch.setenv('GENERIC_OPENAI_API_KEY', 'local-only')

    with pytest.raises(ValueError, match='GENERIC_OPENAI_BASE_URL'):
        providers.get_or_create_openai_compatible_llm('generic', 'local-model')


def test_direct_compatible_provider_never_borrows_the_openai_key(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-leak')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    providers._llm_cache.clear()

    with pytest.raises(ValueError, match='DEEPSEEK_API_KEY'):
        providers.get_or_create_openai_compatible_llm('deepseek', 'deepseek-chat')


@pytest.mark.asyncio
async def test_gateway_registers_generic_deepseek_and_mimo_from_the_shared_provider_contract(monkeypatch):
    for env_name in (
        'OPENAI_API_KEY',
        'DEEPSEEK_API_KEY',
        'MIMO_API_KEY',
        'OPENROUTER_API_KEY',
        'GEMINI_API_KEY',
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv('GENERIC_OPENAI_BASE_URL', 'http://127.0.0.1:11434/v1')
    monkeypatch.setenv('GENERIC_OPENAI_API_KEY', 'local-only')
    registry = dependencies.get_provider_registry()
    try:
        assert registry.provider_for('generic') is not None
        assert registry.provider_for('deepseek') is not None
        assert registry.provider_for('mimo') is not None
        generic = registry.provider_for('generic')
        assert generic is not None
        assert generic._resolved_base_url() == 'http://127.0.0.1:11434/v1'
        assert not any(domain in generic._resolved_base_url() for domain in OFFICIAL_MODEL_DOMAINS)
    finally:
        await dependencies.close_provider_registry()
