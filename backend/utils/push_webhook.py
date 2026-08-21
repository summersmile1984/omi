"""Operator-owned push webhook transport.

This is deliberately a small, explicit bridge rather than an HTTP version of
FCM.  The receiver owns the user/device mapping and the final mobile delivery
adapter.  The backend only sends an opaque registered device token and counts a
request as accepted when the receiver returns a verifiable receipt.

The transport is fail-closed:

* the endpoint must be HTTPS and is DNS-pinned immediately before use;
* the HMAC secret comes from a regular mode-0600 file, never a URL or payload;
* requests are bounded by the shared webhook semaphore/circuit breaker;
* only transport, 429, and 5xx failures are retried, with one idempotency key;
* a 2xx response without the exact receipt contract is not success.

The default self-host profile keeps this provider disabled.  This module is
only reached after startup has selected ``PUSH_PROVIDER=webhook``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import secrets
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

import httpx

from utils.executors import postprocess_executor, submit_with_context
from utils.http_client import (
    UnsafeWebhookURLError,
    get_webhook_circuit_breaker,
    get_webhook_client,
    get_webhook_semaphore,
    safe_request_target,
)

_MAX_SECRET_BYTES = 4096
_MIN_SECRET_BYTES = 32
_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_TOKEN_BYTES = 4096
_MAX_TITLE_BYTES = 1024
_MAX_BODY_BYTES = 16 * 1024
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (0.2, 0.5)

DeliveryStatus = Literal['accepted', 'delivered']


@dataclass(frozen=True)
class PushWebhookConfig:
    endpoint: str
    secret: bytes
    timeout_seconds: float
    max_attempts: int


@dataclass(frozen=True)
class PushWebhookReceipt:
    event_id: str
    receipt_id: str
    status: DeliveryStatus


@dataclass(frozen=True)
class PushWebhookResult:
    """Typed outcome for one device delivery attempt."""

    ok: bool
    retryable: bool
    reason: str
    receipt: PushWebhookReceipt | None = None


def _required(value: str | None, name: str) -> str:
    normalized = (value or '').strip()
    if not normalized:
        raise ValueError(f'{name} is required for PUSH_PROVIDER=webhook')
    return normalized


def _validate_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in parsed.netloc)
    ):
        raise ValueError('PUSH_WEBHOOK_URL must be an HTTPS URL without userinfo, query, or fragment')
    try:
        parsed.port
    except ValueError as error:
        raise ValueError('PUSH_WEBHOOK_URL has an invalid port') from error
    return endpoint.rstrip('/') or endpoint


def _read_secret_file(path_value: str) -> bytes:
    path = Path(path_value)
    try:
        stat_result = path.lstat()
    except OSError as error:
        raise ValueError('PUSH_WEBHOOK_SECRET_FILE is not readable') from error
    if path.is_symlink() or not path.is_file() or (stat_result.st_mode & 0o777) != 0o600:
        raise ValueError('PUSH_WEBHOOK_SECRET_FILE must be a regular non-symlink file with mode 0600')
    try:
        secret = path.read_bytes()
    except OSError as error:
        raise ValueError('PUSH_WEBHOOK_SECRET_FILE is not readable') from error
    if len(secret) > _MAX_SECRET_BYTES:
        raise ValueError('PUSH_WEBHOOK_SECRET_FILE is too large')
    # A trailing newline is a common secret-file convention, but all other
    # whitespace is accidental and would make operator rotations surprising.
    secret = secret.rstrip(b'\r\n')
    if len(secret) < _MIN_SECRET_BYTES or any(byte < 0x20 or byte == 0x7F for byte in secret):
        raise ValueError('PUSH_WEBHOOK_SECRET_FILE must contain at least 32 printable secret bytes')
    return secret


def resolve_push_webhook_config(environ: Mapping[str, str] | None = None) -> PushWebhookConfig:
    """Resolve and validate the operator-owned webhook without network I/O."""

    values = os.environ if environ is None else environ
    endpoint = _validate_endpoint(_required(values.get('PUSH_WEBHOOK_URL'), 'PUSH_WEBHOOK_URL'))
    secret_file = _required(values.get('PUSH_WEBHOOK_SECRET_FILE'), 'PUSH_WEBHOOK_SECRET_FILE')
    secret = _read_secret_file(secret_file)
    try:
        timeout_seconds = float(values.get('PUSH_WEBHOOK_TIMEOUT_SECONDS', '5'))
    except ValueError as error:
        raise ValueError('PUSH_WEBHOOK_TIMEOUT_SECONDS must be a number') from error
    if not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 15:
        raise ValueError('PUSH_WEBHOOK_TIMEOUT_SECONDS must be between 1 and 15 seconds')
    try:
        max_attempts = int(values.get('PUSH_WEBHOOK_MAX_ATTEMPTS', '3'))
    except ValueError as error:
        raise ValueError('PUSH_WEBHOOK_MAX_ATTEMPTS must be an integer') from error
    if not 1 <= max_attempts <= _MAX_ATTEMPTS:
        raise ValueError('PUSH_WEBHOOK_MAX_ATTEMPTS must be between 1 and 3')
    return PushWebhookConfig(endpoint, secret, timeout_seconds, max_attempts)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')
    except (TypeError, ValueError) as error:
        raise ValueError('push webhook payload is not JSON serializable') from error
    if len(body) > _MAX_PAYLOAD_BYTES:
        raise ValueError('push webhook payload exceeds the 64 KiB limit')
    return body


def _signature(secret: bytes, timestamp: str, body: bytes) -> str:
    signed = timestamp.encode('ascii') + b'.' + body
    return 'v1=' + hmac.new(secret, signed, hashlib.sha256).hexdigest()


def _receipt(response: httpx.Response, event_id: str) -> PushWebhookReceipt | None:
    if response.status_code < 200 or response.status_code >= 300:
        return None
    try:
        value = response.json()
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get('schema') != 'omi.push.receipt.v1' or value.get('event_id') != event_id:
        return None
    receipt_id = value.get('receipt_id')
    status = value.get('status')
    if not isinstance(receipt_id, str) or not 1 <= len(receipt_id) <= 256 or status not in {'accepted', 'delivered'}:
        return None
    return PushWebhookReceipt(event_id=event_id, receipt_id=receipt_id, status=status)


def _payload(
    *,
    user_id: str,
    token: str,
    tag: str,
    title: str | None,
    body: str | None,
    data: Mapping[str, Any] | None,
    is_background: bool,
    priority: str,
    event_id: str,
) -> dict[str, Any]:
    if not token or len(token.encode('utf-8')) > _MAX_TOKEN_BYTES:
        raise ValueError('push device token is empty or too large')
    if title is not None and len(title.encode('utf-8')) > _MAX_TITLE_BYTES:
        raise ValueError('push notification title is too large')
    if body is not None and len(body.encode('utf-8')) > _MAX_BODY_BYTES:
        raise ValueError('push notification body is too large')
    return {
        'schema': 'omi.push.webhook.v1',
        'event_id': event_id,
        'user_id': user_id,
        'device': {'token': token, 'token_type': 'opaque_registered_token'},
        'notification': None if title is None and body is None else {'title': title or '', 'body': body or ''},
        'data': dict(data or {}),
        'tag': tag,
        'priority': priority,
        'background': bool(is_background),
    }


async def _send_one(
    config: PushWebhookConfig,
    *,
    user_id: str,
    token: str,
    tag: str,
    title: str | None,
    body: str | None,
    data: Mapping[str, Any] | None,
    is_background: bool,
    priority: str,
) -> PushWebhookResult:
    event_id = secrets.token_hex(16)
    try:
        payload = _payload(
            user_id=user_id,
            token=token,
            tag=tag,
            title=title,
            body=body,
            data=data,
            is_background=is_background,
            priority=priority,
            event_id=event_id,
        )
        body_bytes = _json_bytes(payload)
        pinned_url, pin_kwargs = safe_request_target(config.endpoint)
    except (ValueError, UnsafeWebhookURLError):
        return PushWebhookResult(False, False, 'configuration_or_payload_invalid')

    idempotency_key = f'{event_id}.{hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]}'
    circuit = get_webhook_circuit_breaker(config.endpoint)
    if not circuit.allow_request():
        return PushWebhookResult(False, True, 'transport_circuit_open')

    timestamp = str(int(time.time()))
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'User-Agent': 'omi-push-webhook/1',
        'X-Omi-Push-Event': event_id,
        'X-Omi-Push-Idempotency-Key': idempotency_key,
        'X-Omi-Push-Timestamp': timestamp,
        'X-Omi-Push-Signature': _signature(config.secret, timestamp, body_bytes),
        **pin_kwargs.get('headers', {}),
    }
    extensions = pin_kwargs.get('extensions', {})
    last_reason = 'transport_unavailable'
    for attempt in range(config.max_attempts):
        try:
            async with get_webhook_semaphore():
                response = await get_webhook_client().post(
                    pinned_url,
                    content=body_bytes,
                    headers=headers,
                    extensions=extensions,
                    timeout=config.timeout_seconds,
                )
        except (httpx.TimeoutException, httpx.TransportError):
            circuit.record_failure()
            last_reason = 'transport_unavailable'
            retryable = True
        else:
            receipt = _receipt(response, event_id)
            if receipt is not None:
                circuit.record_success()
                return PushWebhookResult(True, False, 'accepted', receipt)
            retryable = response.status_code == 429 or response.status_code >= 500
            circuit.record_failure()
            last_reason = 'provider_http_error' if response.status_code >= 400 else 'invalid_receipt'
        if not retryable or attempt + 1 >= config.max_attempts:
            return PushWebhookResult(False, retryable, last_reason)
        await asyncio.sleep(_RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)])
    return PushWebhookResult(False, True, last_reason)


async def send_webhook_notifications_async(
    user_id: str,
    tag: str,
    *,
    title: str | None = None,
    body: str | None = None,
    data: Mapping[str, Any] | None = None,
    is_background: bool = False,
    priority: str = 'normal',
    tokens: list[str] | None = None,
) -> int:
    """Send one independently idempotent event to every registered device."""

    config = resolve_push_webhook_config()
    if not tokens:
        return 0
    outcomes = [
        await _send_one(
            config,
            user_id=user_id,
            token=token,
            tag=tag,
            title=title,
            body=body,
            data=data,
            is_background=is_background,
            priority=priority,
        )
        for token in tokens
    ]
    return sum(outcome.ok for outcome in outcomes)


def send_webhook_notifications(
    user_id: str,
    tag: str,
    *,
    title: str | None = None,
    body: str | None = None,
    data: Mapping[str, Any] | None = None,
    is_background: bool = False,
    priority: str = 'normal',
    tokens: list[str] | None = None,
) -> int:
    """Synchronous bridge for the many existing notification call sites."""

    async_call = lambda: asyncio.run(
        send_webhook_notifications_async(
            user_id,
            tag,
            title=title,
            body=body,
            data=data,
            is_background=is_background,
            priority=priority,
            tokens=tokens,
        )
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return async_call()
    future: Future[int] = submit_with_context(postprocess_executor, async_call)
    return future.result()
