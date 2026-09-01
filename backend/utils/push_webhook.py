"""Operator-owned, authenticated notification webhook delivery.

This adapter is deliberately small: it is a notification sink, not a device
push protocol. The receiver owns user/device mapping and must treat the event
id as an idempotency key. No redirect, retry, or vendor fallback is implicit.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from config.push_provider import PushWebhookConfig, push_webhook_config
from utils.http_client import (
    UnsafeWebhookURLError,
    get_webhook_circuit_breaker,
    pin_to_resolved_ip,
    safe_request_target,
)

_DELIVERY_SEMAPHORE = threading.BoundedSemaphore(64)


@dataclass(frozen=True)
class PushWebhookFailure(Exception):
    """Typed, non-sensitive delivery outcome for an operator webhook."""

    code: str
    retryable: bool
    status_code: int | None = None

    def __str__(self) -> str:
        suffix = f' status={self.status_code}' if self.status_code is not None else ''
        return f'{self.code}{suffix}'


@dataclass(frozen=True)
class PushWebhookDelivery:
    event_id: str
    status_code: int


def _event_body(
    *, event_id: str, user_id: str, title: str, body: str, data: Mapping[str, Any] | None, timestamp: str
) -> bytes:
    event = {
        'event': 'notification',
        'event_id': event_id,
        'timestamp': timestamp,
        'user_id': user_id,
        'title': title,
        'body': body,
        'data': dict(data or {}),
    }
    try:
        return json.dumps(event, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')
    except (TypeError, ValueError) as error:
        raise PushWebhookFailure('invalid_payload', retryable=False) from error


def _request_headers(*, config: PushWebhookConfig, body: bytes, event_id: str, timestamp: str) -> dict[str, str]:
    signed = f'{timestamp}.'.encode('ascii') + body
    signature = hmac.new(config.secret.encode('utf-8'), signed, hashlib.sha256).hexdigest()
    return {
        'Content-Type': 'application/json',
        'X-Omi-Event-Id': event_id,
        'X-Omi-Timestamp': timestamp,
        'X-Omi-Signature': f'sha256={signature}',
        'X-Omi-Idempotency-Key': event_id,
    }


def _deliver_with_client(
    *, config: PushWebhookConfig, body: bytes, headers: dict[str, str], client: httpx.Client
) -> PushWebhookDelivery:
    try:
        pinned_url, pin_kwargs = safe_request_target(config.url)
    except UnsafeWebhookURLError as error:
        # Keep resolver details out of the typed error/log surface. The
        # receiver/operator can inspect its endpoint configuration directly.
        raise PushWebhookFailure('ssrf_rejected', retryable=False) from error

    circuit = get_webhook_circuit_breaker(config.url)
    if not circuit.allow_request():
        raise PushWebhookFailure('circuit_open', retryable=True)

    request_headers = dict(headers)
    request_headers.update(pin_kwargs['headers'])
    try:
        with _DELIVERY_SEMAPHORE:
            response = client.post(
                pinned_url,
                content=body,
                headers=request_headers,
                extensions=pin_kwargs['extensions'],
                follow_redirects=False,
                timeout=config.timeout_seconds,
            )
    except httpx.TimeoutException as error:
        circuit.record_failure()
        raise PushWebhookFailure('timeout', retryable=True) from error
    except httpx.RequestError as error:
        circuit.record_failure()
        raise PushWebhookFailure('transport_error', retryable=True) from error

    if 200 <= response.status_code < 300:
        circuit.record_success()
        return PushWebhookDelivery(event_id=headers['X-Omi-Event-Id'], status_code=response.status_code)

    circuit.record_failure()
    retryable = response.status_code == 429 or response.status_code >= 500
    raise PushWebhookFailure(
        'rate_limited' if response.status_code == 429 else 'provider_unavailable' if retryable else 'provider_rejected',
        retryable=retryable,
        status_code=response.status_code,
    )


def deliver_push_webhook(
    user_id: str, title: str, body: str, data: Mapping[str, Any] | None = None
) -> PushWebhookDelivery:
    """Deliver one signed notification synchronously for legacy call sites."""

    config = push_webhook_config()
    event_id = uuid.uuid4().hex
    timestamp = str(int(time.time()))
    payload = _event_body(
        event_id=event_id, user_id=user_id, title=title, body=body, data=data, timestamp=timestamp
    )
    headers = _request_headers(config=config, body=payload, event_id=event_id, timestamp=timestamp)
    # A short-lived client avoids sharing event-loop-bound pools with sync
    # notification call sites. The concurrency and circuit controls above are
    # process-wide and still apply to every delivery.
    with httpx.Client(timeout=config.timeout_seconds) as client:
        return _deliver_with_client(config=config, body=payload, headers=headers, client=client)


async def deliver_push_webhook_async(
    user_id: str, title: str, body: str, data: Mapping[str, Any] | None = None
) -> PushWebhookDelivery:
    """Async boundary that keeps DNS and sync httpx work off the event loop."""

    return await asyncio.to_thread(deliver_push_webhook, user_id, title, body, data)
