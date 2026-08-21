from __future__ import annotations

import json
from typing import Any

import pytest

from utils.retrieval.tools import web_search_tools


class _Response:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> object:
        return self._payload


class _Client:
    def __init__(self, response: _Response):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({'url': url, **kwargs})
        return self.response

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({'url': url, **kwargs})
        return self.response


class _Semaphore:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> None:
        self.entered += 1

    async def __aexit__(self, *_args: object) -> None:
        self.exited += 1


class _CircuitBreaker:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.allow_calls = 0
        self.successes = 0
        self.failures = 0

    def allow_request(self) -> bool:
        self.allow_calls += 1
        return self.allowed

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self) -> None:
        self.failures += 1


@pytest.mark.asyncio
async def test_searxng_transport_executes_explicit_private_service_without_gateway(monkeypatch):
    client = _Client(
        _Response(
            200,
            {
                'results': [
                    {
                        'title': 'Current result',
                        'url': 'https://example.com/current',
                        'content': 'A current public fact.',
                    }
                ]
            },
        )
    )
    monkeypatch.setenv('WEB_SEARCH_TRANSPORT', 'searxng')
    monkeypatch.setenv('SEARXNG_BASE_URL', 'http://searxng:8080')
    semaphore = _Semaphore()
    circuit_breaker = _CircuitBreaker()
    monkeypatch.setattr(web_search_tools, 'get_webhook_client', lambda: client)
    monkeypatch.setattr(web_search_tools, 'get_webhook_semaphore', lambda: semaphore)
    monkeypatch.setattr(web_search_tools, 'get_webhook_circuit_breaker', lambda _url: circuit_breaker)
    monkeypatch.setattr(
        web_search_tools,
        '_gateway_search',
        lambda _query: (_ for _ in ()).throw(AssertionError('must not fall back to the managed gateway')),
    )

    result = await web_search_tools.web_search_tool.coroutine('  latest   release  ')

    assert result == (
        'Web Search Results:\n\n'
        '1. Current result\n'
        '   URL: https://example.com/current\n'
        '   A current public fact.'
    )
    assert client.calls == [
        {
            'url': 'http://searxng:8080/search',
            'params': {'q': 'latest release', 'format': 'json', 'safesearch': '1'},
            'headers': {'Accept': 'application/json'},
            'timeout': 20.0,
        }
    ]
    assert (semaphore.entered, semaphore.exited) == (1, 1)
    assert circuit_breaker.allow_calls == 1
    assert circuit_breaker.successes == 1
    assert circuit_breaker.failures == 0


@pytest.mark.asyncio
async def test_searxng_transport_failure_is_typed_and_never_falls_back(monkeypatch):
    client = _Client(_Response(503, {'error': 'unavailable'}))
    monkeypatch.setenv('WEB_SEARCH_TRANSPORT', 'searxng')
    monkeypatch.setenv('SEARXNG_BASE_URL', 'https://search.example.com')
    semaphore = _Semaphore()
    circuit_breaker = _CircuitBreaker()
    monkeypatch.setattr(web_search_tools, 'get_webhook_client', lambda: client)
    monkeypatch.setattr(web_search_tools, 'get_webhook_semaphore', lambda: semaphore)
    monkeypatch.setattr(web_search_tools, 'get_webhook_circuit_breaker', lambda _url: circuit_breaker)
    gateway_calls: list[str] = []

    async def forbidden_fallback(query: str) -> str:
        gateway_calls.append(query)
        return 'unexpected'

    monkeypatch.setattr(web_search_tools, '_gateway_search', forbidden_fallback)

    result = json.loads(await web_search_tools.web_search_tool.coroutine('current news'))

    assert result == {
        'code': 'model_capability_unavailable',
        'capability': 'web_search',
        'reason': 'transport_http_error',
        'retryable': True,
    }
    assert gateway_calls == []
    assert (semaphore.entered, semaphore.exited) == (1, 1)
    assert circuit_breaker.successes == 0
    assert circuit_breaker.failures == 1


@pytest.mark.asyncio
async def test_searxng_circuit_open_is_typed_before_semaphore_or_client(monkeypatch):
    client = _Client(_Response(200, {'results': []}))
    semaphore = _Semaphore()
    circuit_breaker = _CircuitBreaker(allowed=False)
    monkeypatch.setenv('WEB_SEARCH_TRANSPORT', 'searxng')
    monkeypatch.setenv('SEARXNG_BASE_URL', 'http://searxng:8080')
    monkeypatch.setattr(web_search_tools, 'get_webhook_client', lambda: client)
    monkeypatch.setattr(web_search_tools, 'get_webhook_semaphore', lambda: semaphore)
    monkeypatch.setattr(web_search_tools, 'get_webhook_circuit_breaker', lambda _url: circuit_breaker)

    result = json.loads(await web_search_tools.web_search_tool.coroutine('current news'))

    assert result == {
        'code': 'model_capability_unavailable',
        'capability': 'web_search',
        'reason': 'transport_circuit_open',
        'retryable': True,
    }
    assert client.calls == []
    assert semaphore.entered == 0
    assert circuit_breaker.allow_calls == 1
    assert circuit_breaker.successes == 0
    assert circuit_breaker.failures == 0


@pytest.mark.asyncio
async def test_gateway_transport_uses_semaphore_and_records_success(monkeypatch):
    client = _Client(_Response(200, {'choices': [{'message': {'content': 'current result'}}]}))
    semaphore = _Semaphore()
    circuit_breaker = _CircuitBreaker()
    monkeypatch.setattr(web_search_tools, 'get_llm_gateway_base_url', lambda: 'http://llm-gateway:8080')
    monkeypatch.setattr(web_search_tools, 'feature_auto_lane_id', lambda _feature: 'omi:auto:web-search')
    monkeypatch.setattr(web_search_tools, 'llm_gateway_headers', lambda: {'X-Internal-Key': 'test'})
    monkeypatch.setattr(web_search_tools, 'get_webhook_client', lambda: client)
    monkeypatch.setattr(web_search_tools, 'get_webhook_semaphore', lambda: semaphore)
    monkeypatch.setattr(web_search_tools, 'get_webhook_circuit_breaker', lambda _url: circuit_breaker)

    result = await web_search_tools._gateway_search('current news')

    assert result.startswith('Web Search Results:\n\ncurrent result')
    assert client.calls[0]['url'] == 'http://llm-gateway:8080/v1/chat/completions'
    assert (semaphore.entered, semaphore.exited) == (1, 1)
    assert circuit_breaker.successes == 1
    assert circuit_breaker.failures == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed', [True, False])
async def test_gateway_failure_or_open_circuit_records_the_correct_state(monkeypatch, allowed):
    client = _Client(_Response(503, {'error': 'unavailable'}))
    semaphore = _Semaphore()
    circuit_breaker = _CircuitBreaker(allowed=allowed)
    monkeypatch.setattr(web_search_tools, 'get_llm_gateway_base_url', lambda: 'http://llm-gateway:8080')
    monkeypatch.setattr(web_search_tools, 'get_webhook_client', lambda: client)
    monkeypatch.setattr(web_search_tools, 'get_webhook_semaphore', lambda: semaphore)
    monkeypatch.setattr(web_search_tools, 'get_webhook_circuit_breaker', lambda _url: circuit_breaker)

    result = json.loads(await web_search_tools._gateway_search('current news'))

    assert result['reason'] == ('transport_http_error' if allowed else 'transport_circuit_open')
    assert result['retryable'] is True
    assert len(client.calls) == (1 if allowed else 0)
    assert semaphore.entered == (1 if allowed else 0)
    assert circuit_breaker.failures == (1 if allowed else 0)
    assert circuit_breaker.successes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_mode', ['missing', 'invalid'])
async def test_gateway_invalid_configuration_is_typed_before_breaker_semaphore_or_client(monkeypatch, failure_mode):
    def gateway_url() -> str:
        if failure_mode == 'missing':
            raise RuntimeError('OMI_LLM_GATEWAY_URL required')
        return 'not-a-url'

    monkeypatch.setattr(web_search_tools, 'get_llm_gateway_base_url', gateway_url)
    monkeypatch.setattr(
        web_search_tools,
        'get_webhook_circuit_breaker',
        lambda _url: (_ for _ in ()).throw(AssertionError('invalid config must not allocate a circuit breaker')),
    )
    monkeypatch.setattr(
        web_search_tools,
        'get_webhook_semaphore',
        lambda: (_ for _ in ()).throw(AssertionError('invalid config must not acquire a semaphore')),
    )
    monkeypatch.setattr(
        web_search_tools,
        'get_webhook_client',
        lambda: (_ for _ in ()).throw(AssertionError('invalid config must not construct a client')),
    )

    result = json.loads(await web_search_tools._gateway_search('current news'))

    assert result == {
        'code': 'model_capability_unavailable',
        'capability': 'web_search',
        'reason': 'invalid_transport_configuration',
        'retryable': False,
    }


@pytest.mark.parametrize(
    ('base_url', 'expected'),
    [
        ('http://searxng:8080', 'http://searxng:8080/search'),
        ('http://10.0.0.8:8080', 'http://10.0.0.8:8080/search'),
        ('https://search.example.com/private', 'https://search.example.com/private/search'),
    ],
)
def test_searxng_endpoint_accepts_private_http_or_any_https(base_url, expected):
    assert web_search_tools._searxng_search_url(base_url) == expected


@pytest.mark.parametrize(
    'base_url',
    [
        'http://search.example.com',
        'http://169.254.169.254',
        'ftp://searxng:8080',
        'https://user:secret@search.example.com',
        'https://search.example.com?target=other',
    ],
)
def test_searxng_endpoint_rejects_unsafe_or_ambiguous_configuration(base_url):
    with pytest.raises(ValueError):
        web_search_tools._searxng_search_url(base_url)


def test_searxng_formatter_bounds_results_and_rejects_non_http_urls():
    result = web_search_tools._format_searxng_response(
        {
            'results': [
                {'title': 'ignored', 'url': 'javascript:alert(1)', 'content': 'unsafe'},
                {'title': 'kept', 'url': 'https://example.com', 'content': 'safe'},
                {'title': 'bounded', 'url': 'https://example.org', 'content': 'not emitted'},
            ]
        },
        result_limit=1,
    )

    assert 'javascript:' not in result
    assert 'https://example.com' in result
    assert 'https://example.org' not in result


@pytest.mark.asyncio
async def test_invalid_query_is_rejected_before_transport_selection(monkeypatch):
    monkeypatch.setenv('WEB_SEARCH_TRANSPORT', 'gateway')
    monkeypatch.setattr(
        web_search_tools,
        '_gateway_search',
        lambda _query: (_ for _ in ()).throw(AssertionError('invalid query must not reach a transport')),
    )

    result = json.loads(await web_search_tools.web_search_tool.coroutine('   '))

    assert result['reason'] == 'invalid_query'
    assert result['retryable'] is False
