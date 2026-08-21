from __future__ import annotations

import json
from types import SimpleNamespace

from utils.llm.capabilities import resolve_model_capability
from utils.retrieval.tools import screen_activity_tools


def test_product_model_capability_matrix_has_selected_routes():
    env = {
        'OMI_LLM_DEFAULT_PROVIDER': 'generic',
        'OMI_LLM_DEFAULT_MODEL': 'local-chat',
        'GENERIC_OPENAI_BASE_URL': 'http://model.internal/v1',
        'GENERIC_OPENAI_API_KEY': 'local-key',
        'EMBEDDING_PROVIDER': 'generic',
        'EMBEDDING_MODEL': 'local-embedding',
        'WEB_SEARCH_TRANSPORT': 'gateway',
        'REALTIME_PROVIDER': 'openai',
        'REALTIME_MODEL': 'selected-realtime',
    }

    for capability in ('agent_chat', 'notes', 'memory_kg', 'task_recommendations', 'proactive'):
        route = resolve_model_capability(capability, env=env)
        assert route.selected is True
        assert route.routes
        assert {(candidate.provider, candidate.model) for candidate in route.routes} == {('generic', 'local-chat')}

    assert resolve_model_capability('screen', env=env).routes[0].model == 'local-embedding'
    assert resolve_model_capability('web_search', env=env).transport == 'gateway'
    assert resolve_model_capability('realtime', requested_provider='openai', env=env).routes[0].model == (
        'selected-realtime'
    )


def test_nonportable_capabilities_return_typed_unavailable_payloads():
    screen = resolve_model_capability(
        'screen',
        env={'EMBEDDING_PROVIDER': 'generic', 'EMBEDDING_MODEL': 'local-embedding'},
    )
    web = resolve_model_capability('web_search', env={'WEB_SEARCH_TRANSPORT': 'disabled'})
    realtime = resolve_model_capability(
        'realtime',
        requested_provider='gemini',
        env={'REALTIME_PROVIDER': 'openai'},
    )

    assert json.loads(screen.unavailable_tool_result())['reason'] == 'generic_embedding_endpoint_not_configured'
    assert json.loads(web.unavailable_tool_result())['reason'] == 'disabled_by_deployment'
    assert json.loads(realtime.unavailable_tool_result())['reason'] == 'provider_not_selected'


def test_model_api_and_realtime_relay_require_explicit_transports_and_target_allowlist():
    base = {
        'OMI_LLM_DEFAULT_PROVIDER': 'generic',
        'OMI_LLM_DEFAULT_MODEL': 'local-chat',
        'GENERIC_OPENAI_BASE_URL': 'http://model.internal/v1',
        'GENERIC_OPENAI_API_KEY': 'local-key',
        'EMBEDDING_PROVIDER': 'generic',
        'EMBEDDING_MODEL': 'local-embedding',
    }
    assert resolve_model_capability('embedding', env=base).reason == 'disabled_by_deployment'
    assert resolve_model_capability('proactive_tools', env=base).reason == 'disabled_by_deployment'

    selected = resolve_model_capability(
        'realtime',
        env={
            'REALTIME_PROVIDER': 'relay',
            'REALTIME_MODEL': 'local-live',
            'REALTIME_RELAY_URL': 'ws://realtime.internal/v1/realtime',
            'REALTIME_RELAY_API_KEY': 'server-only',
            'REALTIME_RELAY_PROVIDER_ID': 'generic',
            'REALTIME_RELAY_WIRE_PROTOCOL': 'openai_realtime_v1',
            'REALTIME_RELAY_ALLOWED_HOSTS': 'realtime.internal',
            'REALTIME_RELAY_MAX_MESSAGE_BYTES': '4096',
            'REALTIME_RELAY_MAX_SESSION_SECONDS': '30',
        },
    )
    mismatched_host = resolve_model_capability(
        'realtime',
        env={
            'REALTIME_PROVIDER': 'relay',
            'REALTIME_MODEL': 'local-live',
            'REALTIME_RELAY_URL': 'ws://realtime.internal/v1/realtime',
            'REALTIME_RELAY_API_KEY': 'server-only',
            'REALTIME_RELAY_PROVIDER_ID': 'generic',
            'REALTIME_RELAY_WIRE_PROTOCOL': 'openai_realtime_v1',
            'REALTIME_RELAY_ALLOWED_HOSTS': 'other.internal',
        },
    )

    assert selected.transport == 'websocket_relay'
    assert selected.routes[0].provider == 'generic'
    assert mismatched_host.reason == 'relay_host_not_allowed'

    unsupported_dialect = resolve_model_capability(
        'realtime',
        env={
            'REALTIME_PROVIDER': 'relay',
            'REALTIME_MODEL': 'local-live',
            'REALTIME_RELAY_URL': 'ws://realtime.internal/v1/realtime',
            'REALTIME_RELAY_API_KEY': 'server-only',
            'REALTIME_RELAY_PROVIDER_ID': 'generic',
            'REALTIME_RELAY_WIRE_PROTOCOL': 'vendor_magic_v9',
            'REALTIME_RELAY_ALLOWED_HOSTS': 'realtime.internal',
        },
    )
    assert unsupported_dialect.reason == 'relay_wire_protocol_not_supported'

    metadata_target = resolve_model_capability(
        'realtime',
        env={
            'REALTIME_PROVIDER': 'relay',
            'REALTIME_MODEL': 'local-live',
            'REALTIME_RELAY_URL': 'ws://169.254.169.254/v1/realtime',
            'REALTIME_RELAY_API_KEY': 'server-only',
            'REALTIME_RELAY_PROVIDER_ID': 'generic',
            'REALTIME_RELAY_WIRE_PROTOCOL': 'openai_realtime_v1',
            'REALTIME_RELAY_ALLOWED_HOSTS': '169.254.169.254',
        },
    )
    assert metadata_target.reason == 'relay_host_not_allowed'


def test_self_host_optional_vendor_only_capabilities_are_typed_disabled():
    icon = resolve_model_capability('app_icon_generation', env={'APP_ICON_GENERATION_TRANSPORT': 'disabled'})
    files = resolve_model_capability('file_chat', env={'FILE_CHAT_TRANSPORT': 'disabled'})
    keyless_files = resolve_model_capability('file_chat', env={'FILE_CHAT_TRANSPORT': 'openai_assistants'})
    vendor_proxy = resolve_model_capability('desktop_vendor_proxy', env={'DESKTOP_VENDOR_PROXY_TRANSPORT': 'disabled'})

    assert icon.unavailable_payload()['reason'] == 'disabled_by_deployment'
    assert files.unavailable_payload()['reason'] == 'disabled_by_deployment'
    assert keyless_files.unavailable_payload()['reason'] == 'openai_credential_not_configured'
    assert vendor_proxy.unavailable_payload()['reason'] == 'disabled_by_deployment'


def test_neutral_capability_defaults_fail_closed_without_vendor_routes():
    neutral = {'OMI_DEPLOYMENT_PROFILE': 'self_hosted'}

    assert resolve_model_capability('app_icon_generation', env=neutral).reason == 'disabled_by_deployment'
    assert resolve_model_capability('file_chat', env=neutral).reason == 'disabled_by_deployment'
    assert resolve_model_capability('web_search', env=neutral).reason == 'disabled_by_deployment'
    assert resolve_model_capability('desktop_vendor_proxy', env=neutral).reason == 'disabled_by_deployment'
    assert resolve_model_capability('screen', env=neutral).reason == 'embedding_provider_not_configured'


def test_self_hosted_web_search_selects_only_an_explicit_searxng_endpoint():
    missing = resolve_model_capability('web_search', env={'WEB_SEARCH_TRANSPORT': 'searxng'})
    selected = resolve_model_capability(
        'web_search',
        env={'WEB_SEARCH_TRANSPORT': 'searxng', 'SEARXNG_BASE_URL': 'http://searxng:8080'},
    )

    assert missing.selected is False
    assert missing.reason == 'searxng_endpoint_not_configured'
    assert selected.selected is True
    assert selected.transport == 'searxng'
    assert selected.routes == ()


def test_screen_search_executes_the_selected_embedding_boundary(monkeypatch):
    calls = []
    monkeypatch.setenv('EMBEDDING_PROVIDER', 'generic')
    monkeypatch.setenv('EMBEDDING_MODEL', 'local-embedding')
    monkeypatch.setenv('GENERIC_OPENAI_BASE_URL', 'http://model.internal/v1')
    monkeypatch.setenv('GENERIC_OPENAI_API_KEY', 'local-key')
    monkeypatch.setattr(
        screen_activity_tools,
        'embeddings',
        SimpleNamespace(embed_query=lambda query: calls.append(query) or [1.0, 0.0]),
    )
    monkeypatch.setattr(screen_activity_tools.vector_db, 'search_screen_activity_vectors', lambda **_kwargs: [])

    result = screen_activity_tools.search_screen_activity_tool.invoke(
        {'query': 'budget'},
        config={'configurable': {'user_id': 'uid-1'}},
    )

    assert calls == ['budget']
    assert result.startswith("No screen activity found matching 'budget'.")


def test_screen_search_returns_typed_unavailable_without_a_generic_endpoint(monkeypatch):
    monkeypatch.setenv('EMBEDDING_PROVIDER', 'generic')
    monkeypatch.setenv('EMBEDDING_MODEL', 'local-embedding')
    monkeypatch.delenv('GENERIC_OPENAI_BASE_URL', raising=False)
    monkeypatch.delenv('GENERIC_OPENAI_API_KEY', raising=False)
    monkeypatch.setattr(
        screen_activity_tools,
        'embeddings',
        SimpleNamespace(embed_query=lambda _query: (_ for _ in ()).throw(AssertionError('must not embed'))),
    )

    result = screen_activity_tools.search_screen_activity_tool.invoke(
        {'query': 'budget'},
        config={'configurable': {'user_id': 'uid-1'}},
    )

    assert json.loads(result) == {
        'code': 'model_capability_unavailable',
        'capability': 'screen',
        'reason': 'generic_embedding_endpoint_not_configured',
        'retryable': False,
    }
