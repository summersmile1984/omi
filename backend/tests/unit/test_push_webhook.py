from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest

from utils import push_webhook


def _secret_file(tmp_path: Path, *, mode: int = 0o600) -> Path:
    path = tmp_path / 'push.secret'
    path.write_bytes(b's' * 32)
    path.chmod(mode)
    return path


def _env(secret_file: Path, **overrides: str) -> dict[str, str]:
    values = {
        'PUSH_WEBHOOK_URL': 'https://push.example.test/v1/omi/push',
        'PUSH_WEBHOOK_SECRET_FILE': str(secret_file),
    }
    values.update(overrides)
    return values


def test_resolve_requires_https_and_private_secret_file(tmp_path: Path) -> None:
    secret = _secret_file(tmp_path)
    config = push_webhook.resolve_push_webhook_config(_env(secret))
    assert config.endpoint == 'https://push.example.test/v1/omi/push'
    assert config.secret == b's' * 32
    assert config.max_attempts == 3

    with pytest.raises(ValueError, match='HTTPS URL'):
        push_webhook.resolve_push_webhook_config(_env(secret, PUSH_WEBHOOK_URL='http://push.example.test/push'))

    secret.chmod(0o644)
    with pytest.raises(ValueError, match='mode 0600'):
        push_webhook.resolve_push_webhook_config(_env(secret))


def test_resolve_rejects_symlink_secret(tmp_path: Path) -> None:
    target = _secret_file(tmp_path)
    link = tmp_path / 'link.secret'
    link.symlink_to(target)
    with pytest.raises(ValueError, match='mode 0600'):
        push_webhook.resolve_push_webhook_config(_env(link))


class _Semaphore:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Circuit:
    def __init__(self) -> None:
        self.failures = 0

    def allow_request(self) -> bool:
        return True

    def record_success(self) -> None:
        return None

    def record_failure(self) -> None:
        self.failures += 1


class _Client:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({'url': url, **kwargs})
        return self.responses.pop(0)


def test_send_requires_matching_receipt_and_signs_stable_body(monkeypatch, tmp_path: Path) -> None:
    secret_file = _secret_file(tmp_path)
    config = push_webhook.resolve_push_webhook_config(_env(secret_file, PUSH_WEBHOOK_MAX_ATTEMPTS='1'))
    event_id = '0123456789abcdef0123456789abcdef'
    monkeypatch.setattr(push_webhook.secrets, 'token_hex', lambda _size: event_id)
    client = _Client(
        [
            httpx.Response(
                202,
                json={
                    'schema': 'omi.push.receipt.v1',
                    'event_id': event_id,
                    'receipt_id': 'receipt-1',
                    'status': 'accepted',
                },
            )
        ]
    )
    circuit = _Circuit()
    monkeypatch.setattr(push_webhook, 'safe_request_target', lambda _url: ('https://127.0.0.1/push', {}))
    monkeypatch.setattr(push_webhook, 'get_webhook_client', lambda: client)
    monkeypatch.setattr(push_webhook, 'get_webhook_semaphore', lambda: _Semaphore())
    monkeypatch.setattr(push_webhook, 'get_webhook_circuit_breaker', lambda _url: circuit)

    result = asyncio.run(
        push_webhook._send_one(
            config,
            user_id='user-1',
            token='opaque-device-token',
            tag='tag-1',
            title='Hello',
            body='World',
            data={'type': 'chat'},
            is_background=False,
            priority='high',
        )
    )

    assert result.ok is True
    assert result.receipt is not None
    call = client.calls[0]
    body = call['content']
    assert isinstance(body, bytes)
    payload = json.loads(body)
    assert payload['schema'] == 'omi.push.webhook.v1'
    assert payload['device']['token'] == 'opaque-device-token'
    timestamp = call['headers']['X-Omi-Push-Timestamp']  # type: ignore[index]
    expected = 'v1=' + hmac.new(b's' * 32, f'{timestamp}.'.encode() + body, hashlib.sha256).hexdigest()
    assert call['headers']['X-Omi-Push-Signature'] == expected  # type: ignore[index]
    assert call['headers']['X-Omi-Push-Idempotency-Key'].startswith(event_id + '.')  # type: ignore[index]


def test_send_retries_only_transient_failures_and_reuses_idempotency(monkeypatch, tmp_path: Path) -> None:
    config = push_webhook.resolve_push_webhook_config(_env(_secret_file(tmp_path)))
    client = _Client([httpx.Response(503), httpx.Response(429), httpx.Response(400)])
    circuit = _Circuit()
    monkeypatch.setattr(push_webhook, 'safe_request_target', lambda _url: ('https://127.0.0.1/push', {}))
    monkeypatch.setattr(push_webhook, 'get_webhook_client', lambda: client)
    monkeypatch.setattr(push_webhook, 'get_webhook_semaphore', lambda: _Semaphore())
    monkeypatch.setattr(push_webhook, 'get_webhook_circuit_breaker', lambda _url: circuit)

    result = asyncio.run(
        push_webhook._send_one(
            config,
            user_id='user-1',
            token='token',
            tag='tag',
            title='title',
            body='body',
            data=None,
            is_background=False,
            priority='normal',
        )
    )

    assert result.ok is False
    assert len(client.calls) == 3
    assert len({call['headers']['X-Omi-Push-Idempotency-Key'] for call in client.calls}) == 1  # type: ignore[index]


def test_send_does_not_retry_permanent_http_failure(monkeypatch, tmp_path: Path) -> None:
    config = push_webhook.resolve_push_webhook_config(_env(_secret_file(tmp_path)))
    client = _Client([httpx.Response(401)])
    monkeypatch.setattr(push_webhook, 'safe_request_target', lambda _url: ('https://127.0.0.1/push', {}))
    monkeypatch.setattr(push_webhook, 'get_webhook_client', lambda: client)
    monkeypatch.setattr(push_webhook, 'get_webhook_semaphore', lambda: _Semaphore())
    monkeypatch.setattr(push_webhook, 'get_webhook_circuit_breaker', lambda _url: _Circuit())

    result = asyncio.run(
        push_webhook._send_one(
            config,
            user_id='user-1',
            token='token',
            tag='tag',
            title=None,
            body=None,
            data=None,
            is_background=True,
            priority='high',
        )
    )

    assert result.ok is False
    assert result.retryable is False
    assert len(client.calls) == 1
