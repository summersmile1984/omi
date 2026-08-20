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
