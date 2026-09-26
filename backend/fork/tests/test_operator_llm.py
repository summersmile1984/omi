"""Self-hosted text features resolve to the admitted operator AI; no cloud fallback.

Drives the real upstream owners (utils.llm.model_config, utils.llm.clients)
through the registered patch seam exactly as fork.bootstrap installs it, and
pins the refusal and HTTP contracts of the seam itself.
"""

import asyncio
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest import mock

import pytest

from fork.registry import build_registry


def _admitted_row(vendor: str = 'openrouter') -> dict:
    # Load the production profile renderer, not a second model identity fixture.
    root = Path(__file__).resolve().parents[3]
    with mock.patch.object(sys, 'path', [str(root / 'scripts/profiles'), *sys.path]):
        spec = spec_from_file_location('operator_llm_render_fixture', root / 'scripts/profiles/render.py')
        renderer = module_from_spec(spec)
        spec.loader.exec_module(renderer)
        return renderer.resolve('self_hosted', stage='local', operator_ai=vendor)['profiles']['self_hosted.local']


@pytest.fixture
def admitted(monkeypatch):
    from fork import operator_ai, profile
    from fork.patches.operator_llm import patches as seam

    row = _admitted_row()
    assert 'llm' not in row, 'the admitted self-hosted row must not carry a local llm'
    assert operator_ai.select(row) is not None, 'the admitted self-hosted row selects an operator'
    monkeypatch.setattr(profile, 'current', lambda: row)
    # fork.bootstrap binds these before importing upstream; the tests bind the
    # same gates so get_llm takes the production direct-route branch.
    for name, value in {
        'OMI_LLM_GATEWAY_FEATURE_MODE': 'off',
        'OMI_LLM_CHAT_AGENT_ROUTE': 'direct',
        'OMI_LLM_GATEWAY_DEV_SHADOW_ALL_ENABLED': '0',
        'OMI_LLM_GATEWAY_CONVERSATION_STRUCTURE_SHADOW_ENABLED': '0',
        'OMI_LLM_GATEWAY_CONVERSATION_ACTION_ITEMS_SHADOW_ENABLED': '0',
        'OPENROUTER_API_KEY': 'synthetic-operator-key',
    }.items():
        monkeypatch.setenv(name, value)
    selected = seam()
    originals = [(patch.target()[0], patch.attribute, patch.target()[1]) for patch in selected]
    build_registry(selected).apply(row)
    try:
        yield row
    finally:
        for owner, attribute, original in originals:
            setattr(owner, attribute, original)


def test_upstream_model_resolution_returns_the_admitted_operator(admitted):
    import utils.llm.clients as clients
    from fork import operator_ai
    from fork.operator_chat import HostedChat
    from utils.llm import model_config

    contract = operator_ai.current()
    for feature in ('chat_responses', 'chat_agent', 'conv_structure', 'memory_l1'):
        assert model_config.get_model_config(feature) == (contract.model, contract.provider)
        assert clients._get_model_config(feature) == (contract.model, contract.provider)

    client = clients.get_llm('chat_responses')
    assert isinstance(client, HostedChat)
    assert client.model_name == contract.model
    assert client.openai_api_base == contract.base_url
    assert client.request_timeout == contract.request_timeout_seconds


def test_refusals_fail_closed_without_vendor_fallback(monkeypatch):
    from fork import profile
    from fork.patches.operator_llm import LLMInputRejected, LLMUnavailable, build, route

    monkeypatch.setattr(profile, 'current', lambda: {'target': 'self_hosted'})
    assert route('chat_responses') == ('disabled', 'disabled')
    with pytest.raises(LLMUnavailable, match='no text model'):
        build('any-model', 'openrouter')
    with pytest.raises(LLMInputRejected, match='unknown'):
        route('not-a-configured-feature')


def test_caller_chosen_models_are_refused(admitted):
    from fork.patches.operator_llm import LLMInputRejected, build

    with pytest.raises(LLMInputRejected, match='changed the selected text model'):
        build('gpt-6-luna', 'openai')


def test_byok_request_headers_are_refused_for_the_admitted_contract(admitted):
    import utils.byok as byok
    from fork.patches.operator_llm import LLMInputRejected, route

    contract_model, contract_provider = route('chat_responses')
    assert (contract_model, contract_provider) != ('disabled', 'disabled')
    byok.set_byok_keys({'x-goog-api-key': 'caller-key'})
    try:
        with pytest.raises(LLMInputRejected, match='BYOK'):
            route('chat_responses')
    finally:
        byok.set_byok_keys({})


def test_route_options_retain_the_extractor_schema_contract(admitted):
    from fork.patches.operator_llm import LLMInputRejected, route, route_options
    from utils.llm.working_observations import WorkingObservationBatch

    assert route_options('memory_l1', *route('memory_l1')) == {
        'response_schema': WorkingObservationBatch.model_json_schema()
    }
    assert route_options('chat_responses', *route('chat_responses')) == {}
    with pytest.raises(LLMInputRejected, match='options changed'):
        route_options('chat_responses', 'caller-chosen', 'openai')


def test_selected_agent_retains_real_handlers_and_excludes_cloud_tools():
    from fork.patches.operator_llm import selected_agent

    seen = []

    async def actual(*args):
        seen.append(args)
        return 'actual-loop-result'

    safe, cloud = object(), object()
    schemas = [{'function': {'name': name}} for name in ('get_memories_tool', 'perplexity_web_search')]
    result = asyncio.run(
        selected_agent(actual)(
            'prompt', [], schemas, {'get_memories_tool': safe, 'perplexity_web_search': cloud}, None, [], None, {}
        )
    )
    assert result == 'actual-loop-result'
    prompt, received, registry = seen[0][0], seen[0][2], seen[0][3]
    assert registry == {'get_memories_tool': safe}
    assert received == schemas[:1]
    assert 'deployment_capabilities' in prompt and prompt.startswith('prompt\n<deployment_capabilities>')


def test_text_route_refusals_are_documented_http_responses():
    import importlib

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from fork.capability_transport import install
    from fork.patches.operator_llm import LLMInputRejected, LLMUnavailable

    app = FastAPI()
    for module in (
        'routers.tts',
        'routers.desktop_tts_updates',
        'routers.transcribe',
        'routers.chat',
        'routers.notifications',
    ):
        app.include_router(importlib.import_module(module).router)

    @app.post('/contract/model-rejected')
    def rejected():
        raise LLMInputRejected('caller changed the selected text model')

    @app.post('/contract/model-unavailable')
    def unavailable():
        raise LLMUnavailable('this deployment admits no text model')

    install(app)
    with TestClient(app) as client:
        rejected_response = client.post('/contract/model-rejected')
        unavailable_response = client.post('/contract/model-unavailable')
    assert rejected_response.status_code == 422
    assert rejected_response.json() == {'code': 'model_input_rejected', 'retryable': False}
    assert unavailable_response.status_code == 503
    assert unavailable_response.json() == {'code': 'text_model_unavailable', 'retryable': True}
