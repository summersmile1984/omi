from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, WebSocketException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.server import serve

from database import redis_db
from routers import desktop_realtime, model_capabilities, omni_relay
from utils import model_capability_policy
from utils.llm.embedding_providers import (
    ConfiguredEmbeddingProviderProxy,
    LangChainEmbeddingProviderAdapter,
)
from utils.other import endpoints as auth_endpoints


class _FakeEmbeddingClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def embed_query(self, text):
        return [float(len(text)), 1.0]

    def embed_documents(self, texts):
        return [[float(len(text)), 1.0] for text in texts]


def _configure_generic_embedding(monkeypatch):
    monkeypatch.setenv('EMBEDDING_CAPABILITY_TRANSPORT', 'direct')
    monkeypatch.setenv('EMBEDDING_PROVIDER', 'generic')
    monkeypatch.setenv('EMBEDDING_MODEL', 'local-embedding')
    monkeypatch.setenv('EMBEDDING_DIMENSION', '2')
    monkeypatch.setenv('GENERIC_OPENAI_BASE_URL', 'http://127.0.0.1:11434/v1')
    monkeypatch.setenv('GENERIC_OPENAI_API_KEY', 'local-only-key')
    monkeypatch.setenv('VECTOR_PROJECTION_ACTIVE_VERSION', 'v7')
    monkeypatch.setenv('VECTOR_PROJECTION_SCHEMA_VERSION', '3')
    for key in ('OPENAI_API_KEY', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENROUTER_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr('utils.llm.embedding_providers.OpenAIEmbeddings', _FakeEmbeddingClient)
    default = LangChainEmbeddingProviderAdapter(
        _FakeEmbeddingClient(),
        provider_id='openai',
        model_id='unused-openai',
        dimension=2,
    )
    monkeypatch.setattr(model_capabilities, 'embeddings', ConfiguredEmbeddingProviderProxy(default))


@pytest.mark.asyncio
async def test_embedding_endpoint_runs_generic_provider_and_returns_projection_identity(monkeypatch):
    _configure_generic_embedding(monkeypatch)

    response = await model_capabilities.create_embeddings(
        model_capabilities.EmbeddingCapabilityRequest(
            purpose='ocr',
            mode='document',
            input=['one', 'four'],
            projection_namespace='ns3',
        ),
        uid='uid-1',
    )

    assert response.status_code == 200
    assert response.body
    payload = __import__('json').loads(response.body)
    assert payload['data'] == [
        {'index': 0, 'embedding': [3.0, 1.0]},
        {'index': 1, 'embedding': [4.0, 1.0]},
    ]
    assert payload['projection'] == {
        'provider': 'generic',
        'model': 'local-embedding',
        'dimension': 2,
        'schema_version': 3,
        'namespace_version': 'v7',
        'logical_namespace': 'ns3',
    }


@pytest.mark.asyncio
async def test_embedding_endpoint_fails_closed_before_calling_keyless_generic_provider(monkeypatch):
    _configure_generic_embedding(monkeypatch)
    monkeypatch.delenv('GENERIC_OPENAI_API_KEY')
    calls = []
    monkeypatch.setattr(
        model_capabilities,
        'embeddings',
        SimpleNamespace(
            provider_id='generic',
            model_id='local-embedding',
            dimension=2,
            embed_documents=lambda _texts: calls.append('called'),
        ),
    )

    response = await model_capabilities.create_embeddings(
        model_capabilities.EmbeddingCapabilityRequest(
            purpose='task', mode='document', input=['one'], projection_namespace='ns4'
        ),
        uid='uid-1',
    )

    assert response.status_code == 503
    assert __import__('json').loads(response.body) == {
        'code': 'model_capability_unavailable',
        'capability': 'embedding',
        'reason': 'generic_embedding_endpoint_not_configured',
        'retryable': False,
    }
    assert calls == []


@pytest.mark.asyncio
async def test_embedding_endpoint_rejects_zero_norm_provider_vector_before_projection_persistence(monkeypatch):
    _configure_generic_embedding(monkeypatch)
    monkeypatch.setattr(
        model_capabilities,
        'embeddings',
        SimpleNamespace(
            provider_id='generic',
            model_id='local-embedding',
            dimension=2,
            embed_documents=lambda _texts: [[0.0, 0.0]],
        ),
    )

    response = await model_capabilities.create_embeddings(
        model_capabilities.EmbeddingCapabilityRequest(
            purpose='rewind', mode='document', input=['screen text'], projection_namespace='ns3'
        ),
        uid='uid-1',
    )

    assert response.status_code == 503
    assert __import__('json').loads(response.body) == {
        'code': 'model_capability_unavailable',
        'capability': 'embedding',
        'reason': 'provider_invalid_vector',
        'retryable': False,
    }


def test_embedding_rate_limit_rejects_before_provider_call(monkeypatch):
    _configure_generic_embedding(monkeypatch)
    calls = []
    monkeypatch.setattr(
        model_capabilities,
        'embeddings',
        SimpleNamespace(
            provider_id='generic',
            model_id='local-embedding',
            dimension=2,
            embed_documents=lambda _texts: calls.append('provider-called') or [[1.0, 2.0]],
        ),
    )

    def reject_rate_limit(*_args, **_kwargs):
        raise HTTPException(status_code=429, detail='Rate limit exceeded')

    monkeypatch.setattr(auth_endpoints, '_enforce_rate_limit', reject_rate_limit)
    app = FastAPI()
    app.include_router(model_capabilities.router)
    app.dependency_overrides[model_capabilities.get_current_user_uid] = lambda: 'uid-1'
    response = TestClient(app).post(
        '/v1/model-capabilities/embeddings',
        json={
            'purpose': 'ocr',
            'mode': 'document',
            'input': ['one'],
            'projection_namespace': 'ns3',
        },
    )

    assert response.status_code == 429
    assert calls == []


class _FakeToolModel:
    def __init__(self):
        self.tools = None
        self.tool_choice = None
        self.bound = None
        self.messages = None

    def bind_tools(self, tools, tool_choice):
        self.tools = tools
        self.tool_choice = tool_choice
        return self

    def bind(self, **kwargs):
        self.bound = kwargs
        return self

    def invoke(self, messages):
        self.messages = messages
        return SimpleNamespace(
            content='',
            tool_calls=[{'id': 'call-1', 'name': 'capture_task', 'args': {'title': 'Ship it'}}],
        )


def _configure_generic_tools(monkeypatch):
    monkeypatch.setenv('PROACTIVE_TOOL_TRANSPORT', 'completion')
    monkeypatch.setenv('OMI_LLM_ROUTE_DESKTOP_PROACTIVE_REASONING_PROVIDER', 'generic')
    monkeypatch.setenv('OMI_LLM_ROUTE_DESKTOP_PROACTIVE_REASONING_MODEL', 'local-tools')
    monkeypatch.setenv('GENERIC_OPENAI_BASE_URL', 'http://127.0.0.1:11434/v1')
    monkeypatch.setenv('GENERIC_OPENAI_API_KEY', 'local-only-key')
    monkeypatch.setenv('OMI_LLM_FEATURE_MODE', 'direct')
    for key in ('OPENAI_API_KEY', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENROUTER_API_KEY'):
        monkeypatch.delenv(key, raising=False)

    async def reserve(_uid, operation):
        return model_capabilities.DesktopModelQuotaReservation(
            operation=operation,
            limit=60,
            remaining=59,
            reset_seconds=3600,
        )

    async def release(_uid, _operation):
        return None

    monkeypatch.setattr(model_capabilities, 'reserve_desktop_proactive_quota', reserve)
    monkeypatch.setattr(model_capabilities, 'release_desktop_proactive_quota', release)


@pytest.mark.asyncio
async def test_tool_completion_returns_generic_function_call_without_executing_it(monkeypatch):
    _configure_generic_tools(monkeypatch)
    model = _FakeToolModel()
    monkeypatch.setattr(model_capabilities, '_direct_tool_model', lambda _route: model)

    response = await model_capabilities.create_tool_completion(
        model_capabilities.ToolCompletionRequest(
            messages=[
                {
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': 'Remember this'},
                        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aGVsbG8='}},
                    ],
                }
            ],
            tools=[
                {
                    'type': 'function',
                    'function': {
                        'name': 'capture_task',
                        'description': 'Capture a task',
                        'parameters': {'type': 'object', 'properties': {'title': {'type': 'string'}}},
                    },
                }
            ],
            tool_choice='auto',
            max_output_tokens=321,
        ),
        uid='uid-1',
    )

    assert response.status_code == 200
    payload = __import__('json').loads(response.body)
    assert payload['outcome'] == 'tool_calls'
    assert payload['message']['tool_calls'] == [
        {
            'id': 'call-1',
            'type': 'function',
            'function': {'name': 'capture_task', 'arguments': '{"title":"Ship it"}'},
        }
    ]
    assert payload['route']['primary'] == {'provider': 'generic', 'model': 'local-tools'}
    assert model.bound == {'max_tokens': 321}
    assert response.headers['X-Proactive-Quota-Remaining'] == '59'


@pytest.mark.asyncio
async def test_tool_completion_rejects_remote_image_before_provider_call(monkeypatch):
    _configure_generic_tools(monkeypatch)
    monkeypatch.setattr(
        model_capabilities,
        '_direct_tool_model',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('provider must not be called')),
    )

    with pytest.raises(Exception) as error:
        await model_capabilities.create_tool_completion(
            model_capabilities.ToolCompletionRequest(
                messages=[
                    {
                        'role': 'user',
                        'content': [{'type': 'image_url', 'image_url': {'url': 'https://api.openai.com/private.png'}}],
                    }
                ]
            ),
            uid='uid-1',
        )

    assert getattr(error.value, 'status_code', None) == 422
    assert 'inline' in str(getattr(error.value, 'detail', ''))


@pytest.mark.asyncio
async def test_embedding_endpoint_rejects_a_purpose_namespace_mismatch(monkeypatch):
    _configure_generic_embedding(monkeypatch)

    with pytest.raises(Exception) as error:
        await model_capabilities.create_embeddings(
            model_capabilities.EmbeddingCapabilityRequest(
                purpose='task', mode='document', input=['one'], projection_namespace='ns3'
            ),
            uid='uid-1',
        )

    assert getattr(error.value, 'status_code', None) == 422
    assert getattr(error.value, 'detail', '') == 'projection_namespace for task must be ns4'


@pytest.mark.asyncio
async def test_missing_optional_fallback_does_not_block_configured_generic_primary(monkeypatch):
    _configure_generic_tools(monkeypatch)
    monkeypatch.setenv('OMI_LLM_ROUTE_DESKTOP_PROACTIVE_REASONING_FALLBACKS', 'openai:gpt-5.6-luna')
    model = _FakeToolModel()
    model.invoke = lambda _messages: SimpleNamespace(content='primary answer', tool_calls=[])
    monkeypatch.setattr(model_capabilities, '_direct_tool_model', lambda _route: model)

    response = await model_capabilities.create_tool_completion(
        model_capabilities.ToolCompletionRequest(messages=[{'role': 'user', 'content': 'Choose a tool'}]),
        uid='uid-1',
    )

    assert response.status_code == 200
    payload = __import__('json').loads(response.body)
    assert payload['route']['fallbacks'] == []
    assert payload['route']['unavailable_fallbacks'] == [
        {
            'provider': 'openai',
            'model': 'gpt-5.6-luna',
            'reason': 'openai_credential_not_configured',
        }
    ]


@pytest.mark.asyncio
async def test_tool_request_rejects_unmatched_tool_call_id_and_oversized_schema(monkeypatch):
    _configure_generic_tools(monkeypatch)
    tool = {
        'type': 'function',
        'function': {
            'name': 'capture_task',
            'parameters': {'type': 'object', 'properties': {}},
        },
    }
    unmatched = model_capabilities.ToolCompletionRequest(
        messages=[
            {
                'role': 'assistant',
                'content': '',
                'tool_calls': [
                    {
                        'id': 'call-1',
                        'type': 'function',
                        'function': {'name': 'capture_task', 'arguments': '{}'},
                    }
                ],
            },
            {'role': 'tool', 'tool_call_id': 'different-call', 'content': 'done'},
        ],
        tools=[tool],
    )
    with pytest.raises(HTTPException, match='tool_call_id'):
        await model_capabilities.create_tool_completion(unmatched, uid='uid-1')

    oversized = model_capabilities.ToolCompletionRequest(
        messages=[{'role': 'user', 'content': 'choose'}],
        tools=[
            {
                **tool,
                'function': {
                    **tool['function'],
                    'description': 'x' * (model_capabilities._MAX_TOOL_SCHEMA_BYTES + 1),
                },
            }
        ],
    )
    with pytest.raises(HTTPException, match='tool schemas exceed'):
        await model_capabilities.create_tool_completion(oversized, uid='uid-1')


@pytest.mark.asyncio
async def test_tool_provider_failure_releases_quota_reservation(monkeypatch):
    _configure_generic_tools(monkeypatch)
    released = []

    class FailingModel(_FakeToolModel):
        def invoke(self, messages):
            raise TimeoutError('provider timed out')

    async def release(uid, operation):
        released.append((uid, operation))

    monkeypatch.setattr(model_capabilities, '_direct_tool_model', lambda _route: FailingModel())
    monkeypatch.setattr(model_capabilities, 'release_desktop_proactive_quota', release)

    response = await model_capabilities.create_tool_completion(
        model_capabilities.ToolCompletionRequest(messages=[{'role': 'user', 'content': 'choose'}]),
        uid='uid-1',
    )

    assert response.status_code == 503
    assert released == [('uid-1', 'proactive_reasoning')]


def test_tool_paywall_rejects_before_quota_and_provider(monkeypatch):
    _configure_generic_tools(monkeypatch)
    calls = []

    async def reject_user():
        raise HTTPException(status_code=402, detail='trial_expired')

    async def reserve(*_args):
        calls.append('quota')

    monkeypatch.setattr(model_capabilities, 'reserve_desktop_proactive_quota', reserve)
    monkeypatch.setattr(
        model_capabilities,
        '_direct_tool_model',
        lambda _route: calls.append('provider') or _FakeToolModel(),
    )
    app = FastAPI()
    app.include_router(model_capabilities.router)
    app.dependency_overrides[model_capabilities.authorized_desktop_model_user] = reject_user
    response = TestClient(app).post(
        '/v1/model-capabilities/tool-completions',
        json={'messages': [{'role': 'user', 'content': 'choose'}]},
    )

    assert response.status_code == 402
    assert calls == []


def test_embedding_paywall_rejects_before_rate_limit_and_provider(monkeypatch):
    calls = []

    async def reject_user():
        raise HTTPException(status_code=402, detail='trial_expired')

    monkeypatch.setattr(
        model_capabilities,
        'embeddings',
        type('ForbiddenEmbeddings', (), {'embed_documents': lambda *_args: calls.append('provider')})(),
    )
    monkeypatch.setattr(
        model_capabilities,
        'describe_active_projection',
        lambda **_kwargs: calls.append('projection'),
    )
    app = FastAPI()
    app.include_router(model_capabilities.router)
    app.dependency_overrides[model_capabilities.authorized_desktop_model_user] = reject_user
    response = TestClient(app).post(
        '/v1/model-capabilities/embeddings',
        json={
            'purpose': 'ocr',
            'mode': 'document',
            'input': ['hello'],
            'projection_namespace': 'ns3',
        },
    )

    assert response.status_code == 402
    assert calls == []


@pytest.mark.asyncio
async def test_shared_desktop_model_access_uses_the_trial_paywall_policy():
    async def runner(_executor, checker, uid, platform):
        assert checker('ignored', 'ignored') is True
        assert (uid, platform) == ('uid-1', 'desktop')
        return True

    with pytest.raises(HTTPException) as error:
        await model_capability_policy.enforce_desktop_model_access(
            'uid-1',
            runner=runner,
            paywall_checker=lambda *_args: True,
        )

    assert error.value.status_code == 402


def _configure_relay(monkeypatch, url: str):
    host = url.split('://', 1)[1].split(':', 1)[0]
    monkeypatch.setenv('REALTIME_PROVIDER', 'relay')
    monkeypatch.setenv('REALTIME_RELAY_URL', url)
    monkeypatch.setenv('REALTIME_RELAY_API_KEY', 'server-only-secret')
    monkeypatch.setenv('REALTIME_RELAY_PROVIDER_ID', 'local-realtime')
    monkeypatch.setenv('REALTIME_RELAY_WIRE_PROTOCOL', 'openai_realtime_v1')
    monkeypatch.setenv('REALTIME_RELAY_ALLOWED_HOSTS', host)
    monkeypatch.setenv('REALTIME_MODEL', 'local-realtime-model')
    monkeypatch.setenv('REALTIME_RELAY_MAX_MESSAGE_BYTES', '4096')
    monkeypatch.setenv('REALTIME_RELAY_MAX_SESSION_SECONDS', '30')


@contextmanager
def _echo_websocket_server():
    observed = {'authorization': None, 'messages': []}
    disconnected = threading.Event()

    def handler(connection):
        observed['authorization'] = connection.request.headers.get('Authorization')
        try:
            for message in connection:
                observed['messages'].append(message)
                connection.send(f'echo:{message}')
        finally:
            disconnected.set()

    server = serve(handler, '127.0.0.1', 0)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'ws://127.0.0.1:{port}/v1/realtime', observed, disconnected
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _capability_client(monkeypatch):
    monkeypatch.setattr(model_capabilities, '_verify_ws_auth', lambda authorization: 'uid-1')
    monkeypatch.setattr(model_capabilities, 'enforce_desktop_chat_quota', lambda *_args: None)

    async def admit(_uid, max_session_seconds):
        return model_capability_policy.RealtimeRelayAdmission(
            token='test-relay-lease',
            lease_ttl_seconds=max_session_seconds + 30,
        )

    async def release(_uid, _admission):
        return None

    monkeypatch.setattr(model_capabilities, 'admit_realtime_relay', admit)
    monkeypatch.setattr(model_capabilities, 'release_realtime_relay', release)
    app = FastAPI()
    app.include_router(model_capabilities.router)
    return TestClient(app)


def test_realtime_relay_authenticates_and_forwards_both_directions(monkeypatch):
    with _echo_websocket_server() as (url, observed, disconnected):
        _configure_relay(monkeypatch, url)
        client = _capability_client(monkeypatch)
        releases = []

        async def release(uid, admission):
            releases.append((uid, admission.token))

        monkeypatch.setattr(model_capabilities, 'release_realtime_relay', release)

        with client.websocket_connect(
            '/v1/model-capabilities/realtime/relay',
            headers={'Authorization': 'Bearer client-token'},
            subprotocols=['omi.realtime.v1'],
        ) as websocket:
            assert websocket.accepted_subprotocol == 'omi.realtime.v1'
            websocket.send_text('client-event')
            assert websocket.receive_text() == 'echo:client-event'

        assert disconnected.wait(timeout=5)
        assert observed == {
            'authorization': 'Bearer server-only-secret',
            'messages': ['client-event'],
        }
        assert releases == [('uid-1', 'test-relay-lease')]


def test_realtime_relay_fails_closed_when_disabled(monkeypatch):
    monkeypatch.setenv('REALTIME_PROVIDER', 'disabled')
    client = _capability_client(monkeypatch)

    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            '/v1/model-capabilities/realtime/relay',
            headers={'Authorization': 'Bearer client-token'},
            subprotocols=['omi.realtime.v1'],
        ):
            pass

    assert error.value.code == 1013


def test_realtime_relay_rejects_failed_auth_before_upstream_connect(monkeypatch):
    _configure_relay(monkeypatch, 'ws://127.0.0.1:65530/v1/realtime')

    def reject(_authorization):
        raise WebSocketException(code=1008, reason='Invalid authorization token')

    monkeypatch.setattr(model_capabilities, '_verify_ws_auth', reject)
    app = FastAPI()
    app.include_router(model_capabilities.router)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            '/v1/model-capabilities/realtime/relay',
            headers={'Authorization': 'Bearer invalid'},
            subprotocols=['omi.realtime.v1'],
        ):
            pass

    assert error.value.code == 1008


def test_realtime_relay_rejects_quota_before_upstream_connect(monkeypatch):
    _configure_relay(monkeypatch, 'ws://127.0.0.1:65530/v1/realtime')
    monkeypatch.setattr(model_capabilities, '_verify_ws_auth', lambda _authorization: 'uid-1')

    def reject_quota(*_args):
        raise HTTPException(status_code=429, detail='quota exceeded')

    monkeypatch.setattr(model_capabilities, 'enforce_desktop_chat_quota', reject_quota)
    app = FastAPI()
    app.include_router(model_capabilities.router)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            '/v1/model-capabilities/realtime/relay',
            headers={'Authorization': 'Bearer client-token'},
            subprotocols=['omi.realtime.v1'],
        ):
            pass

    assert error.value.code == 1008
    assert error.value.reason == 'quota_exceeded'


def test_realtime_relay_connect_failure_closes_1013_without_leaking_error(monkeypatch):
    _configure_relay(monkeypatch, 'ws://127.0.0.1:65530/v1/realtime')
    client = _capability_client(monkeypatch)

    with client.websocket_connect(
        '/v1/model-capabilities/realtime/relay',
        headers={'Authorization': 'Bearer client-token'},
        subprotocols=['omi.realtime.v1'],
    ) as websocket:
        with pytest.raises(WebSocketDisconnect) as error:
            websocket.receive_text()

    assert error.value.code == 1013
    assert error.value.reason == 'realtime_upstream_unavailable'


@pytest.mark.parametrize('denial_kind', ['burst', 'lease'])
def test_realtime_relay_rejects_burst_and_lease_denials_before_upstream(monkeypatch, denial_kind):
    _configure_relay(monkeypatch, 'ws://127.0.0.1:65530/v1/realtime')
    client = _capability_client(monkeypatch)
    upstream_calls = []

    async def reject_admission(uid, max_session_seconds):
        async def runner(_executor, fn, *args):
            return fn(*args)

        return await model_capability_policy.admit_realtime_relay(
            uid,
            max_session_seconds,
            runner=runner,
            connect_limiter=lambda *_args: (denial_kind != 'burst', 0, 17),
            lease_acquirer=lambda *_args: denial_kind != 'lease',
        )

    monkeypatch.setattr(model_capabilities, 'admit_realtime_relay', reject_admission)
    monkeypatch.setattr(
        model_capabilities.websockets,
        'connect',
        lambda *_args, **_kwargs: upstream_calls.append('called'),
    )

    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            '/v1/model-capabilities/realtime/relay',
            headers={'Authorization': 'Bearer client-token'},
            subprotocols=['omi.realtime.v1'],
        ):
            pass

    assert error.value.code == 1008
    assert error.value.reason == 'relay_connection_limited'
    assert upstream_calls == []


def test_realtime_relay_releases_lease_when_runtime_configuration_disappears(monkeypatch):
    _configure_relay(monkeypatch, 'ws://127.0.0.1:65530/v1/realtime')
    client = _capability_client(monkeypatch)
    releases = []

    async def admit(_uid, max_session_seconds):
        monkeypatch.delenv('REALTIME_RELAY_API_KEY')
        return model_capability_policy.RealtimeRelayAdmission(
            token='configuration-race-token',
            lease_ttl_seconds=max_session_seconds + 30,
        )

    async def release(uid, admission):
        releases.append((uid, admission.token))

    monkeypatch.setattr(model_capabilities, 'admit_realtime_relay', admit)
    monkeypatch.setattr(model_capabilities, 'release_realtime_relay', release)

    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            '/v1/model-capabilities/realtime/relay',
            headers={'Authorization': 'Bearer client-token'},
            subprotocols=['omi.realtime.v1'],
        ):
            pass

    assert error.value.code == 1013
    assert releases == [('uid-1', 'configuration-race-token')]


def test_realtime_relay_releases_owned_lease_after_upstream_connect_failure(monkeypatch):
    _configure_relay(monkeypatch, 'ws://127.0.0.1:65530/v1/realtime')
    client = _capability_client(monkeypatch)
    releases = []

    async def release(uid, admission):
        releases.append((uid, admission.token))

    monkeypatch.setattr(model_capabilities, 'release_realtime_relay', release)

    with client.websocket_connect(
        '/v1/model-capabilities/realtime/relay',
        headers={'Authorization': 'Bearer client-token'},
        subprotocols=['omi.realtime.v1'],
    ) as websocket:
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_text()

    assert releases == [('uid-1', 'test-relay-lease')]


def test_realtime_relay_session_timeout_releases_owned_lease(monkeypatch):
    with _echo_websocket_server() as (url, _observed, disconnected):
        _configure_relay(monkeypatch, url)
        monkeypatch.setenv('REALTIME_RELAY_MAX_SESSION_SECONDS', '1')
        client = _capability_client(monkeypatch)
        releases = []

        async def release(uid, admission):
            releases.append((uid, admission.token))

        monkeypatch.setattr(model_capabilities, 'release_realtime_relay', release)

        with client.websocket_connect(
            '/v1/model-capabilities/realtime/relay',
            headers={'Authorization': 'Bearer client-token'},
            subprotocols=['omi.realtime.v1'],
        ) as websocket:
            with pytest.raises(WebSocketDisconnect) as error:
                websocket.receive_text()

        assert error.value.code == 1000
        assert error.value.reason == 'session_duration_reached'
        assert disconnected.wait(timeout=5)
        assert releases == [('uid-1', 'test-relay-lease')]


@pytest.mark.asyncio
async def test_realtime_admission_burst_denial_does_not_acquire_lease():
    lease_calls = []

    async def runner(_executor, fn, *args):
        return fn(*args)

    def deny_burst(*_args):
        return False, 0, 17

    with pytest.raises(HTTPException) as error:
        await model_capability_policy.admit_realtime_relay(
            'uid-1',
            1800,
            runner=runner,
            connect_limiter=deny_burst,
            lease_acquirer=lambda *_args: lease_calls.append('lease') or True,
        )

    assert error.value.status_code == 429
    assert error.value.headers == {'Retry-After': '17'}
    assert lease_calls == []


@pytest.mark.asyncio
async def test_realtime_admission_concurrent_lease_denial_is_bounded():
    async def runner(_executor, fn, *args):
        return fn(*args)

    with pytest.raises(HTTPException) as error:
        await model_capability_policy.admit_realtime_relay(
            'uid-1',
            1800,
            runner=runner,
            connect_limiter=lambda *_args: (True, 5, 60),
            lease_acquirer=lambda *_args: False,
        )

    assert error.value.status_code == 429
    assert error.value.headers == {'Retry-After': '1830'}


@pytest.mark.asyncio
async def test_realtime_admission_and_release_preserve_exact_lease_token(monkeypatch):
    observed = []

    async def runner(_executor, fn, *args):
        return fn(*args)

    monkeypatch.setattr(model_capability_policy.secrets, 'token_urlsafe', lambda _size: 'owned-token')
    admission = await model_capability_policy.admit_realtime_relay(
        'uid-1',
        30,
        runner=runner,
        connect_limiter=lambda *_args: (True, 5, 60),
        lease_acquirer=lambda uid, token, ttl: observed.append(('acquire', uid, token, ttl)) or True,
    )
    await model_capability_policy.release_realtime_relay(
        'uid-1',
        admission,
        runner=runner,
        lease_releaser=lambda uid, token: observed.append(('release', uid, token)) or True,
    )

    assert admission == model_capability_policy.RealtimeRelayAdmission(
        token='owned-token',
        lease_ttl_seconds=60,
    )
    assert observed == [
        ('acquire', 'uid-1', 'owned-token', 60),
        ('release', 'uid-1', 'owned-token'),
    ]


def test_realtime_redis_lease_ttl_recovers_and_old_token_cannot_release_new_lease(monkeypatch):
    import fakeredis

    client = fakeredis.FakeRedis(decode_responses=True)
    try:
        release_script = client.register_script(redis_db._REALTIME_RELAY_LEASE_RELEASE_LUA_SOURCE)
        release_script(keys=['probe'], args=['probe'])
    except Exception:
        pytest.skip('fakeredis Lua support unavailable')
    monkeypatch.setattr(redis_db, 'r', client)
    monkeypatch.setattr(redis_db, '_REALTIME_RELAY_LEASE_RELEASE_LUA', release_script)

    assert redis_db.try_acquire_realtime_relay_lease('uid-1', 'old-token', 60) is True
    assert client.ttl('realtime_relay:lease:uid-1') > 0
    assert redis_db.try_acquire_realtime_relay_lease('uid-1', 'new-token', 60) is False

    client.expire('realtime_relay:lease:uid-1', 0)
    assert client.get('realtime_relay:lease:uid-1') is None
    assert redis_db.try_acquire_realtime_relay_lease('uid-1', 'new-token', 60) is True
    assert redis_db.release_realtime_relay_lease('uid-1', 'old-token') is False
    assert client.get('realtime_relay:lease:uid-1') == 'new-token'
    assert redis_db.release_realtime_relay_lease('uid-1', 'new-token') is True
    assert client.get('realtime_relay:lease:uid-1') is None


class _SilentClientWebSocket:
    def __init__(self) -> None:
        self.receive_started = asyncio.Event()
        self.receive_cancelled = asyncio.Event()
        self.closed = []

    async def receive(self):
        self.receive_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.receive_cancelled.set()
            raise

    async def close(self, *, code, reason):
        self.closed.append((code, reason))

    async def send_text(self, _payload):
        return None

    async def send_bytes(self, _payload):
        return None


class _ControllableUpstream:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.receive_started = asyncio.Event()
        self.receive_cancelled = asyncio.Event()
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.receive_started.set()
        if self.error is not None:
            raise self.error
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.receive_cancelled.set()
            raise
        raise StopAsyncIteration

    async def send(self, payload):
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_realtime_pump_times_out_silent_client_and_drains_upstream(monkeypatch):
    monkeypatch.setattr(model_capabilities, 'WS_RECEIVE_TIMEOUT', 0.01)
    websocket = _SilentClientWebSocket()
    upstream = _ControllableUpstream()

    await model_capabilities._pump_realtime(websocket, upstream, 4096, 'silent-uid')

    assert websocket.closed == [(1000, 'client_receive_timeout')]
    assert upstream.receive_cancelled.is_set()
    assert not any('ws:silent-uid:neutral_realtime' in task.get_name() for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_realtime_pump_propagates_upstream_exception_and_drains_client():
    websocket = _SilentClientWebSocket()
    upstream = _ControllableUpstream(error=RuntimeError('upstream failed'))

    with pytest.raises(RuntimeError, match='upstream failed'):
        await model_capabilities._pump_realtime(websocket, upstream, 4096, 'failure-uid')

    assert websocket.receive_cancelled.is_set()
    assert not any('ws:failure-uid:neutral_realtime' in task.get_name() for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_realtime_pump_outer_cancellation_drains_both_directions():
    websocket = _SilentClientWebSocket()
    upstream = _ControllableUpstream()
    pump = asyncio.create_task(model_capabilities._pump_realtime(websocket, upstream, 4096, 'cancel-uid'))
    await websocket.receive_started.wait()
    await upstream.receive_started.wait()

    pump.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pump

    assert websocket.receive_cancelled.is_set()
    assert upstream.receive_cancelled.is_set()
    assert not any('ws:cancel-uid:neutral_realtime' in task.get_name() for task in asyncio.all_tasks())


def test_legacy_omni_relay_cannot_bypass_deployment_relay_authority(monkeypatch):
    _configure_relay(monkeypatch, 'ws://127.0.0.1:65530/v1/realtime')
    calls = []
    monkeypatch.setattr(
        omni_relay,
        '_upstream',
        lambda *_args, **_kwargs: calls.append('vendor') or (_ for _ in ()).throw(AssertionError()),
    )
    app = FastAPI()
    app.include_router(omni_relay.router)

    with pytest.raises(WebSocketDisconnect) as closed:
        with TestClient(app).websocket_connect('/v1/omni/relay?provider=openai'):
            pass

    assert closed.value.code == 1013
    assert closed.value.reason == 'legacy_realtime_transport_unavailable'
    assert calls == []


@pytest.mark.asyncio
async def test_realtime_capability_reports_same_relay_session_contract(monkeypatch):
    _configure_relay(monkeypatch, 'ws://127.0.0.1:9000/v1/realtime')

    response = await model_capabilities.get_realtime_capability(uid='uid-1')

    assert response.status_code == 200
    payload = __import__('json').loads(response.body)
    assert payload == {
        'status': 'selected',
        'capability': 'realtime',
        'transport': 'websocket_relay',
        'protocol': 'omi.realtime.v1',
        'wire_protocol': 'openai_realtime_v1',
        'provider_id': 'local-realtime',
        'model': 'local-realtime-model',
        'session_endpoint': '/v2/realtime/session',
        'websocket_url': '/v1/model-capabilities/realtime/relay',
    }


@pytest.mark.asyncio
async def test_existing_vendor_hint_cannot_override_the_configured_relay(monkeypatch):
    _configure_relay(monkeypatch, 'ws://127.0.0.1:9000/v1/realtime')

    async def no_quota_io(*_args, **_kwargs):
        return None

    monkeypatch.setattr(desktop_realtime, 'run_blocking', no_quota_io)
    relay = await desktop_realtime.mint_session(desktop_realtime.MintRequest(provider='openai'), uid='uid-1')

    assert relay.status_code == 200
    assert __import__('json').loads(relay.body) == {
        'provider': 'relay',
        'provider_id': 'local-realtime',
        'model': 'local-realtime-model',
        'transport': 'websocket_relay',
        'protocol': 'omi.realtime.v1',
        'wire_protocol': 'openai_realtime_v1',
        'websocket_url': '/v1/model-capabilities/realtime/relay',
    }
