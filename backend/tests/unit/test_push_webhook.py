from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace

import httpx
import pytest

from config.push_provider import PushWebhookConfig
from utils import push_webhook


class _FakeClient:
    def __init__(self, response: httpx.Response):
        self.response = response
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, **kwargs):
        self.calls.append({'url': url, **kwargs})
        return self.response


def _config(monkeypatch, **overrides) -> PushWebhookConfig:
    values = {
        'PUSH_PROVIDER': 'webhook',
        'PUSH_WEBHOOK_URL': 'https://notify.example.test/omi',
        'PUSH_WEBHOOK_SECRET': 'operator-secret-1234',
    }
    values.update(overrides)
    monkeypatch.setattr(push_webhook, 'push_webhook_config', lambda: PushWebhookConfig(
        url=values['PUSH_WEBHOOK_URL'],
        secret=values['PUSH_WEBHOOK_SECRET'],
        timeout_seconds=5,
    ))
    return PushWebhookConfig(values['PUSH_WEBHOOK_URL'], values['PUSH_WEBHOOK_SECRET'], 5)


def test_delivery_is_pinned_signed_and_idempotent(monkeypatch) -> None:
    config = _config(monkeypatch)
    fake_client = _FakeClient(httpx.Response(202))
    monkeypatch.setattr(push_webhook.httpx, 'Client', lambda **_kwargs: fake_client)
    monkeypatch.setattr(
        push_webhook,
        'safe_request_target',
        lambda _url: ('https://203.0.113.7/omi', {'headers': {'Host': 'notify.example.test'}, 'extensions': {'sni_hostname': 'notify.example.test'}}),
    )
    circuit = SimpleNamespace(allow_request=lambda: True, record_success=lambda: None, record_failure=lambda: None)
    monkeypatch.setattr(push_webhook, 'get_webhook_circuit_breaker', lambda _url: circuit)

    result = push_webhook.deliver_push_webhook('user-1', 'Title', 'Body', {'kind': 'test'})

    assert result.status_code == 202
    call = fake_client.calls[0]
    assert call['url'] == 'https://203.0.113.7/omi'
    assert call['headers']['Host'] == 'notify.example.test'
    assert call['headers']['X-Omi-Idempotency-Key'] == call['headers']['X-Omi-Event-Id']
    signed = f"{call['headers']['X-Omi-Timestamp']}.".encode() + call['content']
    expected = hmac.new(config.secret.encode(), signed, hashlib.sha256).hexdigest()
    assert call['headers']['X-Omi-Signature'] == f'sha256={expected}'
    assert b'operator-secret-1234' not in call['content']


def test_ssrf_rejection_is_typed_and_does_not_request(monkeypatch) -> None:
    _config(monkeypatch)
    fake_client = _FakeClient(httpx.Response(202))
    monkeypatch.setattr(push_webhook.httpx, 'Client', lambda **_kwargs: fake_client)
    monkeypatch.setattr(
        push_webhook,
        'safe_request_target',
        lambda _url: (_ for _ in ()).throw(push_webhook.UnsafeWebhookURLError('private target')),
    )

    with pytest.raises(push_webhook.PushWebhookFailure) as error:
        push_webhook.deliver_push_webhook('user-1', 'Title', 'Body')

    assert error.value.code == 'ssrf_rejected'
    assert error.value.retryable is False
    assert fake_client.calls == []


def test_provider_5xx_is_retryable_without_exposing_response_body(monkeypatch) -> None:
    _config(monkeypatch)
    fake_client = _FakeClient(httpx.Response(503, content=b'secret receiver details'))
    monkeypatch.setattr(push_webhook.httpx, 'Client', lambda **_kwargs: fake_client)
    monkeypatch.setattr(push_webhook, 'safe_request_target', lambda _url: ('https://203.0.113.7/omi', {'headers': {}, 'extensions': {}}))
    circuit = SimpleNamespace(allow_request=lambda: True, record_success=lambda: None, record_failure=lambda: None)
    monkeypatch.setattr(push_webhook, 'get_webhook_circuit_breaker', lambda _url: circuit)

    with pytest.raises(push_webhook.PushWebhookFailure) as error:
        push_webhook.deliver_push_webhook('user-1', 'Title', 'Body')

    assert error.value.code == 'provider_unavailable'
    assert error.value.retryable is True
    assert 'secret receiver details' not in str(error.value)
