"""Authenticated, provider-neutral model capabilities for desktop workloads."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

import websockets
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, StrictStr

from database.vector_projection import ProjectionUnavailableError, describe_active_projection
from utils.async_tasks import create_named_task, drain_tasks, supervise_tasks
from utils.executors import critical_executor, db_executor, llm_executor, run_blocking
from utils.llm.capabilities import ModelCapabilityRoute, realtime_relay_wire_protocol, resolve_model_capability
from utils.llm.clients import embeddings, get_llm
from utils.llm.direct_fallback import BoundedFallbackChatModel, direct_fallback_reason
from utils.llm.gateway_client import should_route_features_through_gateway
from utils.llm.model_config import ProviderRoute, get_route_options
from utils.llm.providers import get_default_client, get_openai_compatible_provider_config
from utils.model_capability_policy import (
    DesktopModelQuotaReservation,
    admit_realtime_relay,
    authorized_desktop_model_user,
    release_desktop_proactive_quota,
    release_realtime_relay,
    reserve_desktop_proactive_quota,
)
from utils.other.endpoints import (
    _verify_ws_auth,  # type: ignore[reportPrivateUsage]  # shared production WS authentication boundary
    get_current_user_uid,
    with_rate_limit,
)
from utils.subscription import enforce_desktop_chat_quota

router = APIRouter()
logger = logging.getLogger(__name__)

_MAX_EMBEDDING_INPUTS = 32
_MAX_EMBEDDING_CHARACTERS = 200_000
_MAX_TOOL_MESSAGES = 64
_MAX_TOOLS = 32
_MAX_TOOL_TEXT_CHARACTERS = 200_000
_MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_TOOL_SCHEMA_BYTES = 200_000
_MAX_TOOL_REQUEST_BYTES = 8 * 1024 * 1024
_MAX_TOOL_CALL_ID_CHARACTERS = 128
_TOOL_NAME = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_LOGICAL_NAMESPACE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_INLINE_IMAGE = re.compile(r'^data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)$')
_REALTIME_PROTOCOL = 'omi.realtime.v1'
_REALTIME_RELAY_PATH = '/v1/model-capabilities/realtime/relay'
_PURPOSE_PROJECTION_NAMESPACES = {'ocr': 'ns3', 'rewind': 'ns3', 'task': 'ns4'}
WS_RECEIVE_TIMEOUT = 300.0
_REALTIME_TASK_DRAIN_TIMEOUT = 5.0

_embedding_user = with_rate_limit(authorized_desktop_model_user, 'model:embeddings')


class EmbeddingCapabilityRequest(BaseModel):
    purpose: Literal['ocr', 'task', 'rewind']
    mode: Literal['document', 'query']
    input: list[StrictStr]
    projection_namespace: StrictStr


class ToolCompletionRequest(BaseModel):
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: StrictStr | dict[str, Any] | None = None
    max_output_tokens: StrictInt = 1024


def _unavailable(
    route: ModelCapabilityRoute, *, reason: str | None = None, retryable: bool | None = None
) -> JSONResponse:
    payload = route.unavailable_payload()
    if reason is not None:
        payload['reason'] = reason
    if retryable is not None:
        payload['retryable'] = retryable
    return JSONResponse(status_code=503, content=payload)


def _candidate_configuration_error(candidate: ProviderRoute) -> str | None:
    if candidate.provider in {'generic', 'deepseek', 'mimo', 'openai', 'openrouter'}:
        try:
            config = get_openai_compatible_provider_config(candidate.provider)
            config.resolved_base_url()
        except ValueError:
            return f'{candidate.provider}_endpoint_not_configured'
        if not os.getenv(config.api_key_env, '').strip():
            return f'{candidate.provider}_credential_not_configured'
        return None
    if candidate.provider == 'gemini':
        return None if os.getenv('GEMINI_API_KEY', '').strip() else 'gemini_credential_not_configured'
    return f'{candidate.provider}_tool_completion_not_supported'


def _configured_route_error(route: ModelCapabilityRoute) -> str | None:
    """Reject a keyless primary without making optional fallbacks mandatory."""

    if should_route_features_through_gateway():
        return None
    return _candidate_configuration_error(route.routes[0])


def _executable_routes(route: ModelCapabilityRoute) -> tuple[ProviderRoute, ...]:
    if should_route_features_through_gateway():
        return route.routes
    return (route.routes[0],) + tuple(
        candidate for candidate in route.routes[1:] if _candidate_configuration_error(candidate) is None
    )


def _validate_embedding_request(request: EmbeddingCapabilityRequest) -> None:
    if not request.input or len(request.input) > _MAX_EMBEDDING_INPUTS:
        raise HTTPException(status_code=422, detail=f'input must contain 1-{_MAX_EMBEDDING_INPUTS} strings')
    if any(not value.strip() for value in request.input):
        raise HTTPException(status_code=422, detail='input strings must not be blank')
    if sum(len(value) for value in request.input) > _MAX_EMBEDDING_CHARACTERS:
        raise HTTPException(status_code=422, detail='embedding input exceeds the character limit')
    if not _LOGICAL_NAMESPACE.fullmatch(request.projection_namespace):
        raise HTTPException(status_code=422, detail='projection_namespace has an invalid format')
    expected_namespace = _PURPOSE_PROJECTION_NAMESPACES[request.purpose]
    if request.projection_namespace != expected_namespace:
        raise HTTPException(
            status_code=422,
            detail=f'projection_namespace for {request.purpose} must be {expected_namespace}',
        )


def _embed(request: EmbeddingCapabilityRequest) -> list[list[float]]:
    if request.mode == 'query':
        return [embeddings.embed_query(value) for value in request.input]
    return embeddings.embed_documents(request.input)


def _validate_vectors(vectors: Sequence[Sequence[Any]], expected_count: int) -> tuple[list[list[float]], int]:
    if len(vectors) != expected_count:
        raise ProjectionUnavailableError('embedding', 'provider returned the wrong vector count', retryable=False)
    converted: list[list[float]] = []
    dimension: int | None = None
    for vector in vectors:
        if not vector:
            raise ProjectionUnavailableError('embedding', 'provider returned an empty vector', retryable=False)
        values: list[float] = []
        for value in vector:
            if isinstance(value, bool):
                raise ProjectionUnavailableError('embedding', 'provider returned a non-numeric vector', retryable=False)
            try:
                number = float(value)
            except (TypeError, ValueError) as error:
                raise ProjectionUnavailableError(
                    'embedding', 'provider returned a non-numeric vector', retryable=False
                ) from error
            if not math.isfinite(number):
                raise ProjectionUnavailableError('embedding', 'provider returned a non-finite vector', retryable=False)
            values.append(number)
        if dimension is None:
            dimension = len(values)
        elif len(values) != dimension:
            raise ProjectionUnavailableError('embedding', 'provider returned inconsistent dimensions', retryable=False)
        squared_norm = sum(value * value for value in values)
        if squared_norm <= 0 or not math.isfinite(squared_norm):
            raise ProjectionUnavailableError('embedding', 'provider_invalid_vector', retryable=False)
        converted.append(values)
    if dimension is None:
        raise ProjectionUnavailableError('embedding', 'provider returned no vectors', retryable=False)
    return converted, dimension


@router.post('/v1/model-capabilities/embeddings')
async def create_embeddings(
    request: EmbeddingCapabilityRequest,
    uid: str = Depends(_embedding_user),
) -> JSONResponse:
    del uid
    _validate_embedding_request(request)
    route = resolve_model_capability('embedding')
    if not route.selected:
        return _unavailable(route)
    route_error = _configured_route_error(route)
    if route_error:
        return _unavailable(route, reason=route_error, retryable=False)
    if (
        not os.getenv('VECTOR_PROJECTION_ACTIVE_VERSION', '').strip()
        or not os.getenv('VECTOR_PROJECTION_SCHEMA_VERSION', '').strip()
    ):
        return _unavailable(route, reason='projection_identity_not_configured', retryable=False)
    try:
        raw_vectors = await run_blocking(llm_executor, _embed, request)
        vectors, dimension = _validate_vectors(raw_vectors, len(request.input))
        descriptor = describe_active_projection(embeddings, dimension=dimension, capability='embedding')
    except ProjectionUnavailableError as error:
        return _unavailable(route, reason=error.reason, retryable=error.retryable)
    except (ValueError, TypeError):
        return _unavailable(route, reason='provider_not_configured', retryable=False)
    except Exception as error:
        retryable = direct_fallback_reason(error) is not None
        logger.warning('embedding capability execution failed type=%s', type(error).__name__)
        return _unavailable(route, reason='provider_unavailable', retryable=retryable)
    return JSONResponse(
        {
            'status': 'ok',
            'capability': 'embedding',
            'purpose': request.purpose,
            'mode': request.mode,
            'data': [{'index': index, 'embedding': vector} for index, vector in enumerate(vectors)],
            'projection': {
                'provider': descriptor.provider,
                'model': descriptor.model,
                'dimension': descriptor.dimension,
                'schema_version': descriptor.schema_version,
                'namespace_version': descriptor.namespace_version,
                'logical_namespace': request.projection_namespace,
            },
        }
    )


def _inline_image_size(url: str) -> int:
    match = _INLINE_IMAGE.fullmatch(url)
    if match is None:
        raise ValueError('image_url must be an inline PNG, JPEG, or WebP data URL')
    try:
        return len(base64.b64decode(match.group(2), validate=True))
    except (binascii.Error, ValueError) as error:
        raise ValueError('image_url contains invalid base64 data') from error


def _validate_tool_request(request: ToolCompletionRequest) -> None:
    if not request.messages or len(request.messages) > _MAX_TOOL_MESSAGES:
        raise HTTPException(status_code=422, detail=f'messages must contain 1-{_MAX_TOOL_MESSAGES} entries')
    if len(request.tools) > _MAX_TOOLS:
        raise HTTPException(status_code=422, detail=f'tools must contain no more than {_MAX_TOOLS} entries')
    if request.max_output_tokens < 1 or request.max_output_tokens > 8192:
        raise HTTPException(status_code=422, detail='max_output_tokens must be between 1 and 8192')
    try:
        request_size = len(request.model_dump_json().encode())
        tool_schema_size = len(json.dumps(request.tools, separators=(',', ':'), ensure_ascii=False).encode())
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail='tool request must be JSON serializable') from error
    if request_size > _MAX_TOOL_REQUEST_BYTES:
        raise HTTPException(status_code=422, detail='tool request exceeds the serialized byte limit')
    if tool_schema_size > _MAX_TOOL_SCHEMA_BYTES:
        raise HTTPException(status_code=422, detail='tool schemas exceed the serialized byte limit')
    tool_names: set[str] = set()
    for tool in request.tools:
        function = tool.get('function') if tool.get('type') == 'function' else None
        if not isinstance(function, Mapping):
            raise HTTPException(status_code=422, detail='only function tools are supported')
        name = function.get('name')
        parameters = function.get('parameters')
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise HTTPException(status_code=422, detail='tool function name is invalid')
        if name in tool_names:
            raise HTTPException(status_code=422, detail='tool function names must be unique')
        if not isinstance(parameters, Mapping):
            raise HTTPException(status_code=422, detail='tool function parameters must be an object')
        tool_names.add(name)
    if isinstance(request.tool_choice, str) and request.tool_choice not in {'auto', 'none', 'required'}:
        raise HTTPException(status_code=422, detail='tool_choice is invalid')
    if isinstance(request.tool_choice, dict):
        selected = request.tool_choice.get('function')
        name = selected.get('name') if isinstance(selected, Mapping) else None
        if request.tool_choice.get('type') != 'function' or name not in tool_names:
            raise HTTPException(status_code=422, detail='tool_choice references an unknown function')
    total_text = 0
    total_images = 0
    pending_tool_calls: set[str] = set()
    seen_tool_calls: set[str] = set()
    for message in request.messages:
        role = message.get('role')
        if role not in {'system', 'user', 'assistant', 'tool'}:
            raise HTTPException(status_code=422, detail='message role is invalid')
        if set(message) - {'role', 'content', 'name', 'tool_calls', 'tool_call_id'}:
            raise HTTPException(status_code=422, detail='message contains unsupported fields')
        content = message.get('content')
        if isinstance(content, str):
            total_text += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, Mapping):
                    raise HTTPException(status_code=422, detail='message content part must be an object')
                if part.get('type') == 'text' and isinstance(part.get('text'), str):
                    total_text += len(part['text'])
                elif part.get('type') == 'image_url' and role == 'user':
                    image = part.get('image_url')
                    url = image.get('url') if isinstance(image, Mapping) else None
                    if not isinstance(url, str):
                        raise HTTPException(status_code=422, detail='image_url.url must be a string')
                    try:
                        total_images += _inline_image_size(url)
                    except ValueError as error:
                        raise HTTPException(status_code=422, detail=str(error)) from error
                else:
                    raise HTTPException(status_code=422, detail='unsupported message content part')
        elif content is not None:
            raise HTTPException(status_code=422, detail='message content must be text or content parts')
        raw_calls = message.get('tool_calls')
        if raw_calls is not None:
            if role != 'assistant' or not isinstance(raw_calls, list) or len(raw_calls) > _MAX_TOOLS:
                raise HTTPException(status_code=422, detail='assistant tool_calls are invalid')
            for call in raw_calls:
                function = call.get('function') if isinstance(call, Mapping) else None
                call_id = call.get('id') if isinstance(call, Mapping) else None
                name = function.get('name') if isinstance(function, Mapping) else None
                arguments = function.get('arguments') if isinstance(function, Mapping) else None
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or len(call_id) > _MAX_TOOL_CALL_ID_CHARACTERS
                    or call_id in seen_tool_calls
                ):
                    raise HTTPException(status_code=422, detail='assistant tool call id is invalid')
                if call.get('type') != 'function' or not isinstance(name, str) or name not in tool_names:
                    raise HTTPException(status_code=422, detail='assistant tool call function is invalid')
                if not isinstance(arguments, str):
                    raise HTTPException(status_code=422, detail='assistant tool call arguments must be JSON text')
                try:
                    parsed_arguments = json.loads(arguments)
                except json.JSONDecodeError as error:
                    raise HTTPException(
                        status_code=422, detail='assistant tool call arguments are invalid JSON'
                    ) from error
                if not isinstance(parsed_arguments, Mapping):
                    raise HTTPException(status_code=422, detail='assistant tool call arguments must encode an object')
                total_text += len(call_id) + len(name) + len(arguments)
                seen_tool_calls.add(call_id)
                pending_tool_calls.add(call_id)
        if role == 'tool':
            tool_call_id = message.get('tool_call_id')
            if not isinstance(tool_call_id, str) or tool_call_id not in pending_tool_calls:
                raise HTTPException(status_code=422, detail='tool_call_id does not match a prior assistant tool call')
            total_text += len(tool_call_id)
            pending_tool_calls.remove(tool_call_id)
    if total_text > _MAX_TOOL_TEXT_CHARACTERS:
        raise HTTPException(status_code=422, detail='message text exceeds the character limit')
    if total_images > _MAX_INLINE_IMAGE_BYTES:
        raise HTTPException(status_code=422, detail='inline images exceed the byte limit')
    if pending_tool_calls:
        raise HTTPException(status_code=422, detail='assistant tool calls require matching tool messages')


def _direct_tool_model(route: ModelCapabilityRoute) -> Any:
    executable = _executable_routes(route)
    models = []
    for candidate in executable:
        options = {
            **get_route_options('desktop_proactive_reasoning', candidate.model, candidate.provider),
            'request_timeout': 30,
            'max_retries': 0,
        }
        models.append(get_default_client(candidate.model, candidate.provider, False, options))
    if len(models) == 1:
        return models[0]
    return BoundedFallbackChatModel(
        primary=models[0],
        fallback_models=tuple(models[1:]),
        route_labels=tuple(f'{candidate.provider}:{candidate.model}' for candidate in executable),
        feature='desktop_proactive_reasoning',
    )


def _invoke_tool_completion(request: ToolCompletionRequest, route: ModelCapabilityRoute) -> Any:
    model = (
        get_llm('desktop_proactive_reasoning', request_timeout=30, max_retries=0)
        if should_route_features_through_gateway()
        else _direct_tool_model(route)
    )
    runnable: Any = model
    if request.tools:
        bind_options: dict[str, Any] = {'tool_choice': request.tool_choice or 'auto'}
        runnable = model.bind_tools(request.tools, **bind_options)
    runnable = runnable.bind(max_tokens=request.max_output_tokens)
    return runnable.invoke(request.messages)


def _response_content(response: Any) -> str | list[Any]:
    content = getattr(response, 'content', '')
    return content if isinstance(content, (str, list)) else str(content)


def _normalize_response_tool_calls(response: Any, request: ToolCompletionRequest) -> list[dict[str, Any]]:
    raw_calls = getattr(response, 'tool_calls', None) or []
    if not isinstance(raw_calls, list) or len(raw_calls) > _MAX_TOOLS:
        raise ValueError('provider tool calls are invalid')
    declared_names = {str(tool['function']['name']) for tool in request.tools}
    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    total_characters = 0
    for call in raw_calls:
        if not isinstance(call, Mapping):
            raise ValueError('provider tool call is not an object')
        call_id = call.get('id')
        name = call.get('name')
        arguments = call.get('args')
        if (
            not isinstance(call_id, str)
            or not call_id
            or len(call_id) > _MAX_TOOL_CALL_ID_CHARACTERS
            or call_id in seen_ids
        ):
            raise ValueError('provider tool call id is invalid')
        if not isinstance(name, str) or name not in declared_names:
            raise ValueError('provider selected an undeclared tool')
        if not isinstance(arguments, Mapping):
            raise ValueError('provider tool arguments are invalid')
        arguments_json = json.dumps(arguments, separators=(',', ':'), ensure_ascii=False)
        total_characters += len(call_id) + len(name) + len(arguments_json)
        if total_characters > _MAX_TOOL_TEXT_CHARACTERS:
            raise ValueError('provider tool calls exceed the response limit')
        seen_ids.add(call_id)
        normalized.append(
            {
                'id': call_id,
                'type': 'function',
                'function': {'name': name, 'arguments': arguments_json},
            }
        )
    return normalized


@router.post('/v1/model-capabilities/tool-completions')
async def create_tool_completion(
    request: ToolCompletionRequest,
    uid: str = Depends(authorized_desktop_model_user),
) -> JSONResponse:
    _validate_tool_request(request)
    route = resolve_model_capability('proactive_tools')
    if not route.selected:
        return _unavailable(route)
    route_error = _configured_route_error(route)
    if route_error:
        return _unavailable(route, reason=route_error, retryable=False)
    quota: DesktopModelQuotaReservation = await reserve_desktop_proactive_quota(uid, 'proactive_reasoning')
    try:
        response = await run_blocking(llm_executor, _invoke_tool_completion, request, route)
    except (ValueError, TypeError):
        await release_desktop_proactive_quota(uid, 'proactive_reasoning')
        return _unavailable(route, reason='provider_not_configured', retryable=False)
    except Exception as error:
        await release_desktop_proactive_quota(uid, 'proactive_reasoning')
        retryable = direct_fallback_reason(error) is not None
        logger.warning('tool completion capability execution failed type=%s', type(error).__name__)
        return _unavailable(route, reason='provider_unavailable', retryable=retryable)
    try:
        normalized_tool_calls = _normalize_response_tool_calls(response, request)
    except (TypeError, ValueError):
        await release_desktop_proactive_quota(uid, 'proactive_reasoning')
        return _unavailable(route, reason='provider_invalid_response', retryable=False)
    executable = _executable_routes(route)
    executable_set = set(executable)
    return JSONResponse(
        {
            'status': 'ok',
            'capability': 'proactive_tools',
            'outcome': 'tool_calls' if normalized_tool_calls else 'message',
            'message': {
                'role': 'assistant',
                'content': _response_content(response),
                'tool_calls': normalized_tool_calls,
            },
            'route': {
                'feature': 'desktop_proactive_reasoning',
                'primary': {'provider': route.routes[0].provider, 'model': route.routes[0].model},
                'fallbacks': [
                    {'provider': candidate.provider, 'model': candidate.model} for candidate in executable[1:]
                ],
                'unavailable_fallbacks': [
                    {
                        'provider': candidate.provider,
                        'model': candidate.model,
                        'reason': _candidate_configuration_error(candidate),
                    }
                    for candidate in route.routes[1:]
                    if candidate not in executable_set
                ],
            },
        },
        headers=quota.headers(),
    )


def _realtime_contract(route: ModelCapabilityRoute) -> dict[str, Any]:
    if not route.selected:
        return {
            'status': 'unavailable',
            **route.unavailable_payload(),
            'transport': 'none',
            'protocol': _REALTIME_PROTOCOL,
            'wire_protocol': realtime_relay_wire_protocol(),
        }
    selected = route.routes[0]
    if route.transport == 'websocket_relay':
        return {
            'status': 'selected',
            'capability': 'realtime',
            'transport': route.transport,
            'protocol': _REALTIME_PROTOCOL,
            'wire_protocol': realtime_relay_wire_protocol(),
            'provider_id': selected.provider,
            'model': selected.model,
            'session_endpoint': '/v2/realtime/session',
            'websocket_url': _REALTIME_RELAY_PATH,
        }
    return {
        'status': 'selected',
        'capability': 'realtime',
        'transport': route.transport,
        'provider_id': selected.provider,
        'model': selected.model,
        'session_endpoint': '/v2/realtime/session',
    }


@router.get('/v1/model-capabilities/realtime')
async def get_realtime_capability(uid: str = Depends(get_current_user_uid)) -> JSONResponse:
    del uid
    return JSONResponse(_realtime_contract(resolve_model_capability('realtime')))


def _relay_limit(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f'{name} must be an integer') from error
    if value < minimum or value > maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}')
    return value


async def _pump_realtime(websocket: WebSocket, upstream: Any, max_message_bytes: int, uid: str) -> None:
    async def client_to_upstream() -> None:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=WS_RECEIVE_TIMEOUT)
            except TimeoutError:
                await websocket.close(code=1000, reason='client_receive_timeout')
                return
            if message.get('type') == 'websocket.disconnect':
                return
            payload = message.get('text')
            if payload is None:
                payload = message.get('bytes')
            if payload is None:
                continue
            size = len(payload.encode()) if isinstance(payload, str) else len(payload)
            if size > max_message_bytes:
                await websocket.close(code=1009, reason='message_too_large')
                return
            await upstream.send(payload)

    async def upstream_to_client() -> None:
        async for payload in upstream:
            size = len(payload.encode()) if isinstance(payload, str) else len(payload)
            if size > max_message_bytes:
                await websocket.close(code=1009, reason='upstream_message_too_large')
                return
            if isinstance(payload, (bytes, bytearray)):
                await websocket.send_bytes(bytes(payload))
            else:
                await websocket.send_text(payload)

    client_task = create_named_task(client_to_upstream(), name=f'ws:{uid}:neutral_realtime_c2u')
    upstream_task = create_named_task(upstream_to_client(), name=f'ws:{uid}:neutral_realtime_u2c')
    tasks = [client_task, upstream_task]
    try:
        result = await supervise_tasks(
            receive_task=client_task,
            bg_tasks=[upstream_task],
            finite_tasks=None,
            label='neutral_realtime',
        )
        if result.reason == 'crash' and result.exception is not None:
            raise result.exception
        if client_task.done() and not client_task.cancelled():
            exception = client_task.exception()
            if exception is not None:
                raise exception
    finally:
        await drain_tasks(
            tasks,
            timeout=_REALTIME_TASK_DRAIN_TIMEOUT,
            label='neutral_realtime_cleanup',
            cancel=True,
        )


@router.websocket(_REALTIME_RELAY_PATH)
async def realtime_relay(websocket: WebSocket) -> None:
    route = resolve_model_capability('realtime', requested_provider='relay')
    if not route.selected or route.transport != 'websocket_relay':
        await websocket.close(code=1013, reason=(route.reason or 'realtime_unavailable')[:120])
        return
    if _REALTIME_PROTOCOL not in websocket.scope.get('subprotocols', []):
        await websocket.close(code=1008, reason='omi.realtime.v1 subprotocol required')
        return
    try:
        uid = await run_blocking(
            critical_executor,
            _verify_ws_auth,
            cast(str, websocket.headers.get('authorization')),
        )
    except WebSocketException as error:
        await websocket.close(code=error.code, reason=error.reason or 'unauthorized')
        return
    try:
        await run_blocking(db_executor, enforce_desktop_chat_quota, uid, 'desktop')
    except HTTPException as error:
        code = 1008 if error.status_code in {402, 403, 429} else 1013
        reason = 'quota_exceeded' if code == 1008 else 'quota_unavailable'
        await websocket.close(code=code, reason=reason)
        return
    except Exception:
        await websocket.close(code=1013, reason='quota_unavailable')
        return
    try:
        max_message_bytes = _relay_limit('REALTIME_RELAY_MAX_MESSAGE_BYTES', 1_048_576, minimum=1024, maximum=8_388_608)
        max_session_seconds = _relay_limit('REALTIME_RELAY_MAX_SESSION_SECONDS', 1800, minimum=1, maximum=3600)
    except ValueError:
        await websocket.close(code=1013, reason='invalid_relay_limits')
        return
    try:
        admission = await admit_realtime_relay(uid, max_session_seconds)
    except HTTPException as error:
        code = 1008 if error.status_code == 429 else 1013
        reason = 'relay_connection_limited' if code == 1008 else 'relay_admission_unavailable'
        await websocket.close(code=code, reason=reason)
        return
    try:
        url = os.environ['REALTIME_RELAY_URL'].strip()
        api_key = os.environ['REALTIME_RELAY_API_KEY'].strip()
        await websocket.accept(subprotocol=_REALTIME_PROTOCOL)
        async with websockets.connect(
            url,
            extra_headers={'Authorization': f'Bearer {api_key}'},
            max_size=max_message_bytes,
            ping_interval=20,
            ping_timeout=20,
        ) as upstream:
            await asyncio.wait_for(
                _pump_realtime(websocket, upstream, max_message_bytes, uid),
                timeout=max_session_seconds,
            )
    except TimeoutError:
        await websocket.close(code=1000, reason='session_duration_reached')
        return
    except Exception as error:
        logger.warning(
            'realtime relay ended uid=%s provider=%s type=%s',
            uid,
            route.routes[0].provider,
            type(error).__name__,
        )
        try:
            await websocket.close(code=1013, reason='realtime_upstream_unavailable')
        except Exception:
            pass
        return
    finally:
        await release_realtime_relay(uid, admission)
    try:
        await websocket.close(code=1000)
    except Exception:
        pass
