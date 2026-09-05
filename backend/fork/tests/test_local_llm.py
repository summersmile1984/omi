"""Production model/agent boundaries with controlled native protocol responses."""

import asyncio
import json
from unittest import mock

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel

from fork import local_llm
from fork.local_llm_chat import LocalChatModel
from fork.model_contract import validate_llm


def test_mimo_preserves_prompts_tools_and_parses_structured_results():
    from fork.mimo_chat import MiMoChat
    from pydantic import BaseModel

    class Drink(BaseModel):
        drink: str

    sent = []

    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload)
        return httpx.Response(
            200,
            json={
                'id': 'controlled',
                'object': 'chat.completion',
                'created': 1,
                'model': 'mimo-v2.5',
                'choices': [
                    {
                        'index': 0,
                        'finish_reason': 'tool_calls',
                        'message': {
                            'role': 'assistant',
                            'content': None,
                            'tool_calls': [
                                {
                                    'id': 'call-1',
                                    'type': 'function',
                                    'function': {'name': 'Drink', 'arguments': '{"drink":"jasmine tea"}'},
                                }
                            ],
                        },
                    }
                ],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 8, 'total_tokens': 108},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = MiMoChat(
            model='mimo-v2.5',
            api_key='synthetic',
            base_url='https://selected.invalid/v1',
            http_client=client,
            extra_body={'thinking': {'type': 'disabled'}},
        )
        prompt = 'Existing default prompt and full schema descriptions must stay intact.'
        result = model.with_structured_output(Drink).invoke(prompt)
        assert result.drink == 'jasmine tea'
        assert sent[0]['messages'] == [{'content': prompt, 'role': 'user'}]
        assert sent[0]['tool_choice'] == 'auto'
        assert sent[0]['tools'][0]['function']['parameters']['properties']['drink']['type'] == 'string'
        assert sent[0]['thinking'] == {'type': 'disabled'}


def test_mimo_factory_retains_existing_local_sampling_options(monkeypatch):
    from fork import mimo_chat, operator_ai, profile

    row = operator_ai.configure({'target': 'self_hosted', 'stage': 'local', 'capabilities': {}}, 'mimo-cn')
    monkeypatch.setattr(profile, 'current', lambda: row)
    monkeypatch.setenv('MIMO_API_KEY', 'synthetic')
    monkeypatch.delenv('MIMO_SECRET_FILE', raising=False)
    model = mimo_chat.build()
    assert model.temperature == 0
    assert model.extra_body == {'thinking': {'type': 'disabled'}}
    assert model.model_name == 'mimo-v2.5'
    model.http_client.close()


CONTRACT = {
    'provider': 'ollama',
    'model': 'test:fixed',
    'manifest_digest': 'sha256:' + 'a' * 64,
    'artifact_digest': 'sha256:' + 'b' * 64,
    'context_length': 40960,
    'context_window': 32768,
    'max_output_tokens': 8192,
    'runtime_version': '0.33.3',
    'kv_cache_type': 'q8_0',
    'cpu_threads': 4,
    'parallel_requests': 1,
    'request_timeout_seconds': 300,
}


def terminal(text='ready', **changes):
    return {
        'model': 'test:fixed',
        'message': {'role': 'assistant', 'content': text},
        'done': True,
        'done_reason': 'stop',
        'prompt_eval_count': 10,
        'eval_count': 2,
        **changes,
    }


def model(*, answer=None, mutate=None, seen=None, lines=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path == '/api/version':
            data = {'version': '0.33.3'}
        elif path == '/api/tags':
            data = {'models': [{'name': 'test:fixed', 'digest': 'a' * 64}]}
        elif path == '/api/show':
            data = {
                'model_info': {'qwen3.context_length': 40960},
                'capabilities': ['completion', 'tools'],
                'modelfile': 'FROM /models/blobs/sha256-' + 'b' * 64,
            }
        else:
            assert path == '/api/chat'
            payload = json.loads(request.content)
            assert payload['think'] is False
            assert payload['truncate'] is False and payload['shift'] is False
            assert payload['keep_alive'] == 0
            assert payload['options']['num_ctx'] == 32768
            if payload['stream']:
                events = lines if lines is not None else [terminal()]
                return httpx.Response(
                    200, stream=httpx.ByteStream('\n'.join(json.dumps(item) for item in events).encode())
                )
            data = answer if answer is not None else terminal()
        if mutate:
            mutate(path, data)
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(data).encode()))

    return LocalChatModel(
        authority=local_llm.Authority(
            validate_llm(CONTRACT), 'http://localhost', transport=httpx.MockTransport(handler)
        )
    )


def test_sync_async_native_completion_and_usage():
    selected = model()
    assert selected.invoke('hello').content == 'ready'
    result = asyncio.run(selected.ainvoke('hello'))
    assert result.usage_metadata == {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12}


def test_real_schema_parser_receives_native_schema_and_rejects_malformed_output():
    class Fact(BaseModel):
        fact: str

    seen = []
    result = (
        model(answer=terminal('{"fact":"synthetic fact"}'), seen=seen).with_structured_output(Fact).invoke('extract')
    )
    assert result.fact == 'synthetic fact'
    assert json.loads(seen[-1].content)['format'] == Fact.model_json_schema()
    with pytest.raises(Exception):
        model(answer=terminal('{"other":4}')).with_structured_output(Fact).invoke('extract')
    raw = model(answer=terminal('invalid JSON')).with_structured_output(Fact, include_raw=True).invoke('extract')
    assert raw['parsed'] is None and raw['parsing_error'] is not None


def test_streaming_tool_calls_and_results_preserve_real_langchain_wire():
    calls = {
        'role': 'assistant',
        'content': '',
        'tool_calls': [{'function': {'name': 'get_memories_tool', 'arguments': {'query': 'tea'}}}],
    }
    selected = model(lines=[terminal('', message=calls)])

    async def stream():
        return [part async for part in selected.astream('What do I prefer?')]

    chunks = asyncio.run(stream())
    call = chunks[0].tool_calls[0]
    assert call['name'] == 'get_memories_tool' and call['args'] == {'query': 'tea'}
    seen = []
    model(seen=seen).invoke(
        [
            HumanMessage(content='preferences'),
            AIMessage(content='', tool_calls=[call]),
            ToolMessage(content='tea', tool_call_id=call['id']),
        ]
    )
    assert json.loads(seen[-1].content)['messages'][-1] == {
        'role': 'tool',
        'content': 'tea',
        'tool_name': 'get_memories_tool',
    }


@pytest.mark.parametrize(
    'path,field,value',
    [
        ('/api/version', 'version', 'latest'),
        ('/api/tags', 'models', []),
        ('/api/show', 'model_info', {'qwen3.context_length': 2048}),
        ('/api/show', 'modelfile', 'FROM changed'),
        ('/api/show', 'capabilities', ['completion']),
        ('/api/chat', 'model', 'another:tag'),
        ('/api/chat', 'done', False),
        ('/api/chat', 'done_reason', 'length'),
        ('/api/chat', 'eval_count', -1),
        ('/api/chat', 'message', {'role': 'assistant', 'content': ''}),
    ],
)
def test_identity_and_terminal_faults_are_not_success(path, field, value):
    def mutate(actual, data):
        if actual == path:
            data[field] = value

    with pytest.raises(local_llm.LLMUnavailable):
        model(mutate=mutate).invoke('hello')


def test_stream_eof_after_partial_text_is_failure():
    async def consume():
        outputs = []
        with pytest.raises(local_llm.LLMUnavailable, match='completed'):
            async for part in model(lines=[terminal('partial', done=False)]).astream('hello'):
                outputs.append(part.content)
        assert outputs == ['partial']

    asyncio.run(consume())


def test_context_and_nontext_rejected_before_any_network():
    seen = []
    selected = model(seen=seen)
    for messages in [
        'x' * 262145,
        [HumanMessage(content=[{'type': 'image_url', 'image_url': 'https://example.invalid'}])],
    ]:
        with pytest.raises(local_llm.LLMInputRejected):
            selected.invoke(messages)
    assert not seen


def test_selected_profile_disabled_and_byok_have_no_vendor_fallback(monkeypatch):
    from fork.capabilities import CapabilityDisabled
    import utils.byok as byok

    monkeypatch.setattr(local_llm, 'current', lambda: {'target': 'self_hosted'})
    assert local_llm.route('chat_responses') == ('disabled', 'disabled')
    with pytest.raises(CapabilityDisabled):
        local_llm.build(*local_llm.route('chat_responses')).invoke('legacy user greeting')
    monkeypatch.setattr(byok, 'has_byok_keys', lambda: True)
    with pytest.raises(local_llm.LLMInputRejected, match='BYOK'):
        local_llm.route('chat_responses')


def test_captured_feature_factory_uses_selected_owner(monkeypatch):
    import utils.llm.clients as clients
    from fork.patches.llm import patches
    from fork.registry import build_registry

    # Keep the factory captured before installation, as upstream feature modules do.
    captured = clients.get_llm
    selected = model()
    monkeypatch.setattr(
        local_llm,
        'current',
        lambda: {'target': 'self_hosted', 'llm': CONTRACT, 'capabilities': {'llm_provider': 'ollama'}},
    )
    monkeypatch.setattr(local_llm, 'build', lambda *args, **kwargs: selected)
    monkeypatch.setenv('OMI_LLM_GATEWAY_FEATURE_MODE', 'off')
    monkeypatch.setenv('OMI_LLM_CHAT_AGENT_ROUTE', 'direct')
    monkeypatch.setenv('OMI_LLM_GATEWAY_DEV_SHADOW_ALL_ENABLED', '0')
    selected_patches = patches()[:-1]
    original = [(patch.target()[0], patch.attribute, patch.target()[1]) for patch in selected_patches]
    try:
        build_registry(selected_patches).apply({'target': 'self_hosted'})
        for feature in ('chat_responses', 'chat_agent', 'conv_structure', 'memory_l1'):
            assert captured(feature).invoke('test').content == 'ready'
    finally:
        for owner, attr, value in original:
            setattr(owner, attr, value)


def test_selected_agent_retains_real_handlers_and_excludes_cloud_tools():
    from fork.patches.llm import selected_agent

    seen = []

    async def actual(*args):
        seen.append(args)
        return 'actual-loop-result'

    safe, cloud = object(), object()
    schemas = [{'function': {'name': name}} for name in ('get_memories_tool', 'perplexity_web_search')]
    assert (
        asyncio.run(
            selected_agent(actual)(
                'prompt', [], schemas, {'get_memories_tool': safe, 'perplexity_web_search': cloud}, None, [], None, {}
            )
        )
        == 'actual-loop-result'
    )
    assert seen[0][3] == {'get_memories_tool': safe} and seen[0][2] == schemas[:1]


@pytest.mark.parametrize('failed', [False, True])
def test_actual_persona_consumers_receive_factory_selected_stream_tokens(monkeypatch, failed):
    from types import SimpleNamespace
    from langchain_core.callbacks import BaseCallbackHandler
    from fork.patches.llm import patches
    from fork.registry import build_registry
    from utils.llm import persona
    from utils.retrieval import graph
    from utils.llm import usage_tracker

    seen, records = [], []
    lines = [terminal('hello ', done=False), terminal('friend')]
    if failed:
        lines = lines[:1]
    selected = model(lines=lines, seen=seen)
    monkeypatch.setattr(local_llm, 'Authority', lambda *args, **kwargs: selected.authority)
    monkeypatch.setattr(
        local_llm,
        'current',
        lambda: {'target': 'self_hosted', 'llm': CONTRACT, 'capabilities': {'llm_provider': 'ollama'}},
    )
    monkeypatch.setattr(
        usage_tracker,
        'get_usage_callback',
        lambda: usage_tracker.LLMUsageCallback(flush_fn=lambda *args: records.append(args)),
    )
    monkeypatch.setenv('OMI_LLM_GATEWAY_FEATURE_MODE', 'off')
    monkeypatch.setenv('OMI_LLM_GATEWAY_DEV_SHADOW_ALL_ENABLED', '0')
    monkeypatch.setattr(graph, 'get_chat_tracer_callbacks', lambda **kwargs: [])
    selected_patches = patches()[:-1]
    for patch in selected_patches:
        owner, original = patch.target()
        monkeypatch.setattr(owner, patch.attribute, original)
    build_registry(selected_patches).apply({'target': 'self_hosted'})
    app = SimpleNamespace(
        id='existing-persona', name='User persona', persona_prompt='A user supplied persona', is_influencer=False
    )

    async def run_graph():
        details = {}
        chunks = [part async for part in graph.execute_persona_chat_stream('old-user', [], app, callback_data=details)]
        if failed:
            assert any(part and part.startswith('error: ') for part in chunks)
            assert details['error'] == 'stream_failure'
        else:
            assert ''.join(part[6:] for part in chunks if part and part.startswith('data: ')) == 'hello friend'
            assert details['answer'] == 'hello friend'
            assert not details.get('error')

    asyncio.run(run_graph())
    tokens = []

    class Callback(BaseCallbackHandler):
        def on_llm_new_token(self, token, **kwargs):
            tokens.append(token)

    if failed:
        with pytest.raises(local_llm.LLMUnavailable, match='completed'):
            persona.answer_persona_question_stream('old-user', app, [], [Callback()])
        assert ''.join(tokens) == 'hello '
        assert not records
    else:
        assert persona.answer_persona_question_stream('old-user', app, [], [Callback()]) == 'hello friend'
        assert ''.join(tokens) == 'hello friend'
        assert len(records) == 2
    assert all(json.loads(request.content)['stream'] for request in seen if request.url.path == '/api/chat')


def test_native_usage_reaches_existing_callback_exactly_once_and_failed_stream_never_charges():
    from fork.llm_usage import UsageCallback
    from utils.llm.usage_tracker import LLMUsageCallback as ExistingUsageCallback

    records = []
    delegated = ExistingUsageCallback(flush_fn=lambda *args: records.append(args))
    selected = model()
    selected.callbacks = [UsageCallback(delegated, 'test:fixed')]
    selected.invoke('test')
    assert len(records) == 1 and records[0][-3:] == ('test:fixed', 10, 2)

    async def consume():
        async for _ in selected.astream('test'):
            pass

    asyncio.run(consume())
    assert len(records) == 2 and records[1][-3:] == ('test:fixed', 10, 2)
    selected = model(lines=[terminal('partial', done=False)])
    selected.callbacks = [UsageCallback(delegated, 'test:fixed')]
    with pytest.raises(local_llm.LLMUnavailable):
        asyncio.run(consume())
    assert len(records) == 2


def test_sync_invoke_inside_async_context_preserves_uid_and_usage():
    from contextvars import ContextVar

    marker = ContextVar('llm_test_principal', default='missing')
    seen = []

    def observe(path, data):
        seen.append(marker.get())

    async def call():
        marker.set('synthetic-existing-principal')
        return model(mutate=observe).invoke('test').content

    assert asyncio.run(call()) == 'ready'
    assert set(seen) == {'synthetic-existing-principal'}


def test_sync_socket_deadline_shrinks_on_each_read_and_rejects_slow_drip(monkeypatch):
    from fork import llm_http

    clock, timeouts = [10.0], []

    class Socket:
        def read(self, size, timeout):
            timeouts.append(timeout)
            clock[0] += 0.6
            return b'one chunk'

    monkeypatch.setattr(llm_http.time, 'monotonic', lambda: clock[0])
    stream = llm_http.DeadlineStream(Socket(), 11.0)
    assert stream.read(64) == b'one chunk'
    with pytest.raises(httpx.TimeoutException):
        stream.read(64)
    assert timeouts == pytest.approx([1.0, 0.4])


def test_json_and_newline_free_streams_are_bounded_before_full_read():
    from fork.llm_http import Frames, LIMIT, request_json

    consumed = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for part in (b'x' * LIMIT, b'x', b'never consumed'):
                consumed.append(len(part))
                yield part

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Body()))) as client:
        with pytest.raises(local_llm.LLMUnavailable, match='limit'):
            request_json(client, 'GET', 'http://localhost', '/api/tags', float('inf'))
    assert consumed == [LIMIT, 1]
    frames = Frames()
    assert list(frames.add(b'x' * LIMIT)) == []
    with pytest.raises(local_llm.LLMUnavailable, match='limit'):
        list(frames.add(b'x'))
    assert len(frames.pending) == LIMIT


def test_stream_cancellation_closes_actual_http_response_without_terminal_usage():
    closed = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (json.dumps(terminal('partial', done=False)) + '\n').encode()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    selected = model()
    original = selected.authority.transport

    async def handle(request):
        if request.url.path == '/api/chat':
            return httpx.Response(200, stream=Body())
        return await original.handle_async_request(request)

    selected.authority.transport = httpx.MockTransport(handle)

    async def consume():
        stream = selected.astream('test')
        first = await anext(stream)
        assert first.content == 'partial' and first.usage_metadata is None
        task = asyncio.create_task(anext(stream))
        # Synchronization uses an event-loop turn, not a wall-clock sleep.
        started = asyncio.Event()
        asyncio.get_running_loop().call_soon(started.set)
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stream.aclose()

    asyncio.run(consume())
    assert closed == [True]


@pytest.mark.parametrize('operation', ['connect', 'tls'])
def test_late_acquired_socket_is_closed_before_deadline_error(monkeypatch, operation):
    from fork import llm_http

    clock, closed = [10.0], []

    class Acquired:
        def close(self):
            closed.append(True)

    class Backend:
        def connect_tcp(self, *args, **kwargs):
            clock[0] = 12.0
            return Acquired()

        def start_tls(self, *args, **kwargs):
            clock[0] = 12.0
            return Acquired()

    monkeypatch.setattr(llm_http.time, 'monotonic', lambda: clock[0])
    with pytest.raises(httpx.TimeoutException):
        if operation == 'connect':
            llm_http.DeadlineBackend(11.0, Backend()).connect_tcp('fixture.invalid', 80)
        else:
            llm_http.DeadlineStream(Backend(), 11.0).start_tls(None)
    assert closed == [True]


def test_existing_agent_retry_owner_preserves_local_error_and_partial_stream_boundaries():
    from utils.retrieval.safety import should_retry_provider_error, provider_fallback_reason

    budget = dict(attempts_made=1, max_attempts=3, seconds_remaining=60, min_headroom_seconds=10)
    unavailable = local_llm.LLMUnavailable('controlled provider fault')
    rejected = local_llm.LLMInputRejected('controlled input refusal')
    assert should_retry_provider_error(unavailable, text_already_streamed=False, **budget)
    assert not should_retry_provider_error(unavailable, text_already_streamed=True, **budget)
    assert not should_retry_provider_error(rejected, text_already_streamed=False, **budget)
    assert provider_fallback_reason(unavailable) == 'provider_5xx'


def test_selected_budget_overrides_cloud_default_and_cannot_exceed_profile(monkeypatch):
    monkeypatch.setattr(local_llm, 'contract_for_profile', lambda: validate_llm(CONTRACT))
    monkeypatch.setenv('LLM_ENDPOINT', 'http://localhost:11434')
    monkeypatch.setattr(local_llm, 'assert_http_endpoint_allowed', lambda url: None)
    selected = local_llm.build(CONTRACT['model'], 'ollama', options={'request_timeout': 60})
    assert selected.authority.timeout == 300
    assert selected.payload([HumanMessage('test')], None, False, {})['options']['num_thread'] == 4
    for field, value in [('cpu_threads', 0), ('parallel_requests', 2), ('request_timeout_seconds', 0)]:
        with pytest.raises(ValueError, match='budget'):
            validate_llm({**CONTRACT, field: value})


@pytest.mark.parametrize('invalid', [False, True])
def test_captured_memory_extractor_uses_native_original_schema_and_preserves_parse_failure(monkeypatch, invalid):
    from fork.patches.llm import patches
    from fork.registry import build_registry
    from models.memory_contracts import WorkingObservationExtractionError
    from utils.llm import working_observations as owner

    seen = []
    answer = json.dumps(
        {'items': [{'text': 'The user prefers jasmine tea.', 'confidence': 0.9 if invalid else 'high'}]}
    )
    selected = model(answer=terminal(answer), seen=seen)
    monkeypatch.setattr(
        local_llm,
        'current',
        lambda: {'target': 'self_hosted', 'llm': CONTRACT, 'capabilities': {'llm_provider': 'ollama'}},
    )
    monkeypatch.setattr(
        local_llm,
        'build',
        lambda model, provider, streaming, options: selected.model_copy(
            update={'response_schema': options.get('response_schema')}
        ),
    )
    monkeypatch.setenv('OMI_LLM_GATEWAY_FEATURE_MODE', 'off')
    monkeypatch.setenv('OMI_LLM_GATEWAY_DEV_SHADOW_ALL_ENABLED', '0')
    selected_patches = patches()[:-1]
    originals = [(patch.target()[0], patch.attribute, patch.target()[1]) for patch in selected_patches]
    try:
        build_registry(selected_patches).apply({'target': 'self_hosted'})

        def extract():
            return owner.extract_l1_memory_archive_items_from_text(
                uid='existing-user',
                source_id='recording',
                source_type='voice_transcript',
                text='I prefer jasmine tea instead of coffee.',
                persist_route_outcomes=False,
                strict=True,
            )

        if invalid:
            with pytest.raises(WorkingObservationExtractionError, match='parse'):
                extract()
        else:
            assert extract()[0].text == 'The user prefers jasmine tea.'
        payload = json.loads(next(request.content for request in seen if request.url.path == '/api/chat'))
        assert payload['format'] == owner.WorkingObservationBatch.model_json_schema()
    finally:
        for module, name, value in originals:
            setattr(module, name, value)
