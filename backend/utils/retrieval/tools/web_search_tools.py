"""Provider-neutral web-search tool with explicit deployment routing."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from langchain_core.tools import tool  # type: ignore[reportUnknownVariableType]  # langchain @tool decorator partially typed
from utils.http_client import get_webhook_circuit_breaker, get_webhook_client, get_webhook_semaphore
from utils.llm.capabilities import resolve_model_capability
from utils.llm.gateway_client import feature_auto_lane_id, get_llm_gateway_base_url, llm_gateway_headers
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

WEB_SEARCH_TRANSPORT_GATEWAY = 'gateway'
WEB_SEARCH_TRANSPORT_SEARXNG = 'searxng'

_PRIVATE_SERVICE_HOST = re.compile(r'^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$')
_PRIVATE_SERVICE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', 'fc00::/7', '::1/128')
)
_MAX_QUERY_CHARACTERS = 2048
_MAX_RESULT_CHARACTERS = 1600


@tool
async def web_search_tool(query: str) -> str:
    """Search the web for current public information using the deployment-selected transport.

    Use this for current events, recent facts, or questions that require public
    information newer than the model's training data. Do not use it for the
    user's private conversations, memories, or action items.
    """

    normalized_query = ' '.join(query.split())
    if not normalized_query:
        return _tool_error('invalid_query', retryable=False)
    if len(normalized_query) > _MAX_QUERY_CHARACTERS:
        return _tool_error('query_too_long', retryable=False)

    capability = resolve_model_capability('web_search')
    if not capability.selected:
        logger.error('Web search capability unavailable: %s', capability.reason)
        return capability.unavailable_tool_result()
    if capability.transport == WEB_SEARCH_TRANSPORT_GATEWAY:
        return await _gateway_search(normalized_query)
    if capability.transport == WEB_SEARCH_TRANSPORT_SEARXNG:
        return await _searxng_search(normalized_query)
    return _tool_error('unsupported_transport', retryable=False)


async def _searxng_search(query: str) -> str:
    base_url = os.environ.get('SEARXNG_BASE_URL', '').strip().rstrip('/')
    try:
        search_url = _searxng_search_url(base_url)
        result_limit = _bounded_result_limit(os.environ.get('SEARXNG_RESULT_LIMIT', '8'))
        timeout = _bounded_timeout(os.environ.get('SEARXNG_TIMEOUT_SECONDS', '20'))
    except ValueError as error:
        logger.error('SearXNG configuration invalid: %s', error)
        return _tool_error('invalid_transport_configuration', retryable=False)

    circuit_breaker = get_webhook_circuit_breaker(search_url)
    if not circuit_breaker.allow_request():
        logger.warning('Web search transport circuit is open')
        return _tool_error('transport_circuit_open', retryable=True)

    try:
        async with get_webhook_semaphore():
            response = await get_webhook_client().get(
                search_url,
                params={'q': query, 'format': 'json', 'safesearch': '1'},
                headers={'Accept': 'application/json'},
                timeout=timeout,
            )
        if response.status_code != 200:
            circuit_breaker.record_failure()
            logger.error('Web search transport returned status %s', response.status_code)
            return _tool_error(
                'transport_http_error', retryable=response.status_code >= 500 or response.status_code == 429
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('response root must be an object')
        result = _format_searxng_response(cast(dict[str, Any], payload), result_limit=result_limit)
        circuit_breaker.record_success()
        return result
    except httpx.TimeoutException:
        circuit_breaker.record_failure()
        logger.warning('Web search transport timed out')
        return _tool_error('transport_timeout', retryable=True)
    except httpx.HTTPError as error:
        circuit_breaker.record_failure()
        logger.error('Web search transport request failed: %s', type(error).__name__)
        return _tool_error('transport_request_error', retryable=True)
    except (ValueError, KeyError, TypeError):
        circuit_breaker.record_failure()
        logger.error('Web search transport returned an invalid response')
        return _tool_error('invalid_transport_response', retryable=False)


async def _gateway_search(query: str) -> str:
    try:
        url = _gateway_search_url()
    except (RuntimeError, TypeError, ValueError):
        logger.error('Managed web search configuration is invalid')
        return _tool_error('invalid_transport_configuration', retryable=False)
    circuit_breaker = get_webhook_circuit_breaker(url)
    if not circuit_breaker.allow_request():
        logger.warning('Managed web search circuit is open')
        return _tool_error('transport_circuit_open', retryable=True)

    try:
        async with get_webhook_semaphore():
            response = await get_webhook_client().post(
                url,
                json={
                    'model': feature_auto_lane_id('web_search'),
                    'messages': [{'role': 'user', 'content': query}],
                    'temperature': 0.2,
                    'max_tokens': 1000,
                },
                headers=llm_gateway_headers(),
                timeout=30.0,
            )
        if response.status_code != 200:
            circuit_breaker.record_failure()
            logger.error(
                'Managed web search returned status %s: %s', response.status_code, sanitize(response.text[:200])
            )
            return _tool_error(
                'transport_http_error', retryable=response.status_code >= 500 or response.status_code == 429
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('response root must be an object')
        result = _format_gateway_response(cast(dict[str, Any], payload))
        circuit_breaker.record_success()
        return result
    except httpx.TimeoutException:
        circuit_breaker.record_failure()
        logger.warning('Managed web search timed out')
        return _tool_error('transport_timeout', retryable=True)
    except httpx.HTTPError as error:
        circuit_breaker.record_failure()
        logger.error('Managed web search request failed: %s', type(error).__name__)
        return _tool_error('transport_request_error', retryable=True)
    except (ValueError, IndexError, KeyError, TypeError):
        circuit_breaker.record_failure()
        logger.error('Managed web search returned an invalid response')
        return _tool_error('invalid_transport_response', retryable=False)


def _gateway_search_url() -> str:
    base_url = get_llm_gateway_base_url().strip().rstrip('/')
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError('LLM gateway base URL must be an HTTP(S) URL without credentials, query, or fragment')
    return f'{base_url}/v1/chat/completions'


def _searxng_search_url(base_url: str) -> str:
    base_url = base_url.rstrip('/')
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError('SEARXNG_BASE_URL must be an HTTP(S) origin without credentials, query, or fragment')
    if parsed.scheme == 'http' and not _is_private_service_host(parsed.hostname or ''):
        raise ValueError('plain HTTP is restricted to an explicit private service host')
    return f'{base_url}/search'


def _is_private_service_host(host: str) -> bool:
    normalized = host.rstrip('.').lower()
    if not normalized:
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        if '.' not in normalized:
            return _PRIVATE_SERVICE_HOST.fullmatch(normalized) is not None
        return normalized.endswith(('.internal', '.svc', '.svc.cluster.local'))
    return any(address in network for network in _PRIVATE_SERVICE_NETWORKS if address.version == network.version)


def _bounded_result_limit(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError('SEARXNG_RESULT_LIMIT must be an integer') from error
    if value < 1 or value > 10:
        raise ValueError('SEARXNG_RESULT_LIMIT must be between 1 and 10')
    return value


def _bounded_timeout(raw_value: str) -> float:
    try:
        value = float(raw_value)
    except ValueError as error:
        raise ValueError('SEARXNG_TIMEOUT_SECONDS must be a number') from error
    if value <= 0 or value > 30:
        raise ValueError('SEARXNG_TIMEOUT_SECONDS must be greater than 0 and at most 30')
    return value


def _format_searxng_response(result: dict[str, Any], *, result_limit: int) -> str:
    raw_results = result.get('results')
    if not isinstance(raw_results, list):
        raise ValueError('missing results list')

    formatted: list[str] = []
    for raw_item in cast(list[object], raw_results):
        item = cast(dict[str, object], raw_item) if isinstance(raw_item, dict) else None
        if not isinstance(item, dict):
            continue
        title = _bounded_text(item.get('title'))
        url = _safe_result_url(item.get('url'))
        content = _bounded_text(item.get('content'))
        if not url or (not title and not content):
            continue
        lines = [f'{len(formatted) + 1}. {title or url}', f'   URL: {url}']
        if content:
            lines.append(f'   {content}')
        formatted.append('\n'.join(lines))
        if len(formatted) >= result_limit:
            break

    if not formatted:
        return 'Web Search Results:\n\nNo results found.'
    return 'Web Search Results:\n\n' + '\n\n'.join(formatted)


def _format_gateway_response(result: dict[str, Any]) -> str:
    if 'choices' in result and len(result['choices']) > 0:
        content: Any = result['choices'][0]['message']['content']
        formatted_result = f'Web Search Results:\n\n{content}\n\n'
        citations = _extract_citations(result)
        if citations:
            formatted_result += '\nSources:\n'
            for index, citation in enumerate(citations[:10], 1):
                if isinstance(citation, dict):
                    typed_citation = cast(dict[str, Any], citation)
                    url = typed_citation.get('url', typed_citation.get('citation', ''))
                    title = typed_citation.get('title', '')
                    if url:
                        formatted_result += f'{index}. {title}\n   {url}\n'
                elif isinstance(citation, str):
                    formatted_result += f'{index}. {citation}\n'
        return formatted_result.strip()
    raise ValueError('missing choices')


def _extract_citations(result: dict[str, Any]) -> list[Any]:
    citations: Any = result.get('citations') or result.get('search_results')
    if citations:
        return citations
    return result.get('choices', [{}])[0].get('message', {}).get('citations', [])


def _bounded_text(value: object) -> str:
    if not isinstance(value, str):
        return ''
    return ' '.join(value.split())[:_MAX_RESULT_CHARACTERS]


def _safe_result_url(value: object) -> str:
    if not isinstance(value, str):
        return ''
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username is not None:
        return ''
    return value[:2048]


def _tool_error(reason: str, *, retryable: bool) -> str:
    return json.dumps(
        {
            'code': 'model_capability_unavailable',
            'capability': 'web_search',
            'reason': reason,
            'retryable': retryable,
        },
        sort_keys=True,
        separators=(',', ':'),
    )
