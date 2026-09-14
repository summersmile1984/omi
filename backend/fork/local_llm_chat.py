"""LangChain text/schema/tool adapter for the selected Ollama native protocol."""

import asyncio
import json
import time
import uuid
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.output_parsers import JsonOutputParser, PydanticOutputParser
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict

from .local_llm import Authority, LLMInputRejected, LLMUnavailable
from .llm_http import DeadlineTransport, Frames, arequest_json, remaining, request_json, validate_response


def content_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(part, dict) and part.get('type') == 'text' for part in value):
        return '\n'.join(part['text'] for part in value)
    raise LLMInputRejected('selected local LLM supports text only')


def messages_wire(messages):
    result = []
    for message in messages:
        roles = {HumanMessage: 'user', SystemMessage: 'system', AIMessage: 'assistant', ToolMessage: 'tool'}
        role = next((role for cls, role in roles.items() if isinstance(message, cls)), None)
        if role is None:
            raise LLMInputRejected('unsupported local LLM message role')
        row = {'role': role, 'content': content_text(message.content)}
        if isinstance(message, AIMessage) and message.tool_calls:
            row['tool_calls'] = [
                {'function': {'name': call['name'], 'arguments': call['args']}} for call in message.tool_calls
            ]
        if isinstance(message, ToolMessage):
            row['tool_name'] = message.name or next(
                (
                    call['name']
                    for prior in messages
                    if isinstance(prior, AIMessage)
                    for call in prior.tool_calls
                    if call['id'] == message.tool_call_id
                ),
                '',
            )
            if not row['tool_name']:
                raise LLMInputRejected('tool result has no matching assistant call')
        result.append(row)
    return result


class StreamState:
    def __init__(self, model):
        self.model, self.complete, self.emitted, self.call_index = model, False, False, 0

    def accept(self, data):
        if self.complete:
            raise LLMUnavailable('local stream continued after completion')
        text, calls, usage = self.model.decode(data)
        self.emitted = self.emitted or bool(text.strip() or calls)
        self.complete = usage is not None
        chunk = AIMessageChunk(
            content=text,
            tool_call_chunks=[
                {
                    'name': call['name'],
                    'args': json.dumps(call['args']),
                    'id': call['id'],
                    'index': self.call_index + index,
                }
                for index, call in enumerate(calls)
            ],
            usage_metadata=usage,
        )
        self.call_index += len(calls)
        return ChatGenerationChunk(
            message=chunk,
            generation_info={'model_name': self.model.authority.contract.model} if self.complete else None,
        )

    def finish(self):
        if not self.complete or not self.emitted:
            raise LLMUnavailable('local stream ended without a completed answer')


class LocalChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    authority: Authority | None
    response_schema: dict | None = None
    streaming: bool = False

    @property
    def _llm_type(self):
        return 'selected-ollama'

    @property
    def _identifying_params(self):
        return (
            {'model_name': self.authority.contract.model, 'provider': 'ollama'}
            if self.authority
            else {'model_name': 'disabled', 'provider': 'disabled'}
        )

    def payload(self, messages, stop, stream, kwargs):
        if self.authority is None:
            from .capabilities import reject

            reject('llm')
        contract = self.authority.contract
        allowed = {'tools', 'tool_choice', 'max_completion_tokens', 'max_tokens', 'format', 'stream'}
        if set(kwargs) - allowed:
            raise LLMInputRejected('unsupported local model request option')
        if kwargs.get('tool_choice', 'auto') != 'auto':
            raise LLMInputRejected('local model requires automatic tool selection')
        maximum = kwargs.get('max_completion_tokens', kwargs.get('max_tokens', contract.max_output_tokens))
        if type(maximum) is not int or maximum < 1:
            raise LLMInputRejected('output limit must be a positive integer')
        # Caller max_tokens is an upper bound, not a promise to generate that
        # many tokens; the selected serving contract may impose a lower cap.
        maximum = min(maximum, contract.max_output_tokens)
        rows = messages_wire(messages)
        tools = kwargs.get('tools', [])
        if len(json.dumps([rows, tools, kwargs.get('format')], ensure_ascii=False).encode()) > 256 * 1024:
            raise LLMInputRejected('input exceeds the bounded local model request size')
        options = {
            'num_ctx': contract.context_window,
            'num_predict': maximum,
            'num_thread': contract.cpu_threads,
            'num_batch': 128,
            'temperature': 0,
        }
        if stop:
            options['stop'] = stop
        payload = {
            'model': contract.model,
            'messages': rows,
            'stream': stream,
            'think': False,
            'truncate': False,
            'shift': False,
            'keep_alive': 0,
            'options': options,
        }
        if tools:
            payload['tools'] = tools
        format_value = kwargs.get('format', self.response_schema)
        if self.response_schema is not None and format_value != self.response_schema:
            raise LLMInputRejected('caller changed the feature response schema')
        if format_value is not None:
            payload['format'] = format_value
        return payload

    def decode(self, data):
        if not isinstance(data, dict) or data.get('model') != self.authority.contract.model or data.get('error'):
            raise LLMUnavailable('local generation identity or envelope differs')
        message = data.get('message')
        if (
            not isinstance(message, dict)
            or message.get('role') != 'assistant'
            or not isinstance(message.get('content'), str)
        ):
            raise LLMUnavailable('local generation message is malformed')
        calls = message.get('tool_calls', [])
        if not isinstance(calls, list):
            raise LLMUnavailable('local tool calls are malformed')
        normalized = []
        for call in calls:
            function = call.get('function') if isinstance(call, dict) else None
            if (
                not isinstance(function, dict)
                or not isinstance(function.get('name'), str)
                or not isinstance(function.get('arguments'), dict)
            ):
                raise LLMUnavailable('local tool call is malformed')
            normalized.append(
                {'name': function['name'], 'args': function['arguments'], 'id': str(uuid.uuid4()), 'type': 'tool_call'}
            )
        usage = None
        if data.get('done') is True:
            if data.get('done_reason') != 'stop':
                raise LLMUnavailable('local generation stopped without a complete answer')
            counts = [data.get('prompt_eval_count'), data.get('eval_count')]
            if any(type(count) is not int or count < 0 for count in counts):
                raise LLMUnavailable('local terminal usage is malformed')
            usage = {'input_tokens': counts[0], 'output_tokens': counts[1], 'total_tokens': sum(counts)}
        return message['content'], normalized, usage

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        payload = self.payload(messages, stop, False, kwargs)
        authority = self.authority
        started = time.monotonic()
        try:
            with httpx.Client(
                transport=authority.transport or DeadlineTransport(started + authority.timeout),
                timeout=authority.timeout,
                follow_redirects=False,
                headers={'Accept-Encoding': 'identity'},
            ) as client:
                authority.identity(client, started + authority.timeout)
                data = request_json(
                    client, 'POST', authority.endpoint, '/api/chat', started + authority.timeout, payload
                )
            text, calls, usage = self.decode(data)
            if usage is None or not (text.strip() or calls):
                raise LLMUnavailable('local model returned no completed answer')
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(content=text, tool_calls=calls, usage_metadata=usage),
                        generation_info={'model_name': authority.contract.model},
                    )
                ]
            )
        except httpx.HTTPError as error:
            raise LLMUnavailable('local model transport unavailable') from error

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        payload = self.payload(messages, stop, False, kwargs)
        authority = self.authority
        try:
            async with asyncio.timeout(authority.timeout):
                async with httpx.AsyncClient(
                    transport=authority.transport,
                    timeout=authority.timeout,
                    follow_redirects=False,
                    headers={'Accept-Encoding': 'identity'},
                ) as client:
                    await authority.aidentity(client)
                    data = await arequest_json(client, 'POST', authority.endpoint, '/api/chat', payload)
            text, calls, usage = self.decode(data)
            if usage is None or not (text.strip() or calls):
                raise LLMUnavailable('local model returned no completed answer')
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(content=text, tool_calls=calls, usage_metadata=usage),
                        generation_info={'model_name': authority.contract.model},
                    )
                ]
            )
        except (httpx.HTTPError, TimeoutError) as error:
            raise LLMUnavailable('local model transport unavailable') from error

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        payload = self.payload(messages, stop, True, kwargs)
        authority = self.authority
        deadline = time.monotonic() + authority.timeout
        state = StreamState(self)
        try:
            with httpx.Client(
                transport=authority.transport or DeadlineTransport(deadline),
                timeout=authority.timeout,
                follow_redirects=False,
                headers={'Accept-Encoding': 'identity'},
            ) as client:
                authority.identity(client, deadline)
                with client.stream('POST', authority.endpoint + '/api/chat', json=payload) as response:
                    validate_response(response, '/api/chat')
                    frames = Frames()
                    for raw in response.iter_raw():
                        remaining(deadline)
                        for data in frames.add(raw):
                            yield state.accept(data)
                    remaining(deadline)
                    for data in frames.finish():
                        yield state.accept(data)
            state.finish()
        except (httpx.HTTPError, json.JSONDecodeError) as error:
            raise LLMUnavailable('local model stream unavailable or malformed') from error

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        payload = self.payload(messages, stop, True, kwargs)
        authority = self.authority
        state = StreamState(self)
        try:
            async with asyncio.timeout(authority.timeout):
                async with httpx.AsyncClient(
                    transport=authority.transport,
                    timeout=authority.timeout,
                    follow_redirects=False,
                    headers={'Accept-Encoding': 'identity'},
                ) as client:
                    await authority.aidentity(client)
                    async with client.stream('POST', authority.endpoint + '/api/chat', json=payload) as response:
                        validate_response(response, '/api/chat')
                        frames = Frames()

                        async def events():
                            async for chunk in response.aiter_raw():
                                for item in frames.add(chunk):
                                    yield item
                            for item in frames.finish():
                                yield item

                        async for data in events():
                            yield state.accept(data)
            state.finish()
        except (httpx.HTTPError, TimeoutError, json.JSONDecodeError) as error:
            raise LLMUnavailable('local model stream unavailable or malformed') from error

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(
            tools=[convert_to_openai_tool(tool) for tool in tools], tool_choice=tool_choice or 'auto', **kwargs
        )

    def with_structured_output(self, schema, *, include_raw=False, **kwargs):
        if kwargs:
            raise LLMInputRejected('unsupported structured output option')
        is_model = isinstance(schema, type) and issubclass(schema, BaseModel)
        model = self.bind(format=schema.model_json_schema() if is_model else schema)
        parser = PydanticOutputParser(pydantic_object=schema) if is_model else JsonOutputParser()
        if not include_raw:
            return model | parser
        parse = RunnablePassthrough.assign(
            parsed=RunnableLambda(lambda item: item['raw']) | parser, parsing_error=lambda _: None
        )
        failed = RunnablePassthrough.assign(parsed=lambda _: None)
        return {'raw': model} | parse.with_fallbacks([failed], exception_key='parsing_error')
