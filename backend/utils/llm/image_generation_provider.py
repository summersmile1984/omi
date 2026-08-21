"""Explicit OpenAI-compatible image-generation transport.

This transport has no default endpoint or credential.  It is suitable for an
operator-owned image service implementing ``POST /v1/images/generations`` and
returning base64 image data.
"""

from __future__ import annotations

from collections.abc import Mapping
import base64
from io import BytesIO
import hashlib
import os
from typing import cast
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw

from utils.http_client import get_webhook_circuit_breaker
from utils.llm.capabilities import ModelCapabilityUnavailableError


def generate_image_via_local_template(*, prompt: str, size: str = '1024x1024') -> Mapping[str, object]:
    """Render a deterministic neutral icon without a model or network call."""

    if size != '1024x1024':
        raise ModelCapabilityUnavailableError('app_icon_generation', 'unsupported_image_size', retryable=False)
    digest = hashlib.sha256(prompt.encode('utf-8')).digest()
    # Grayscale-only palette is brand-safe and cannot drift into the forbidden
    # purple family. The prompt hash varies composition without exposing text.
    background = 18 + digest[0] % 28
    foreground = 196 + digest[1] % 52
    accent = 92 + digest[2] % 72
    image = Image.new('RGB', (1024, 1024), (background, background, background))
    draw = ImageDraw.Draw(image)
    margin = 128 + digest[3] % 48
    radius = 150 + digest[4] % 70
    draw.rounded_rectangle(
        (margin, margin, 1024 - margin, 1024 - margin),
        radius=radius,
        fill=(foreground, foreground, foreground),
    )
    inset = margin + 140 + digest[5] % 50
    width = 44 + digest[6] % 36
    draw.line(
        (inset, 512, 512, inset, 1024 - inset, 512, 512, 1024 - inset, inset, 512),
        fill=(accent, accent, accent),
        width=width,
        joint='curve',
    )
    output = BytesIO()
    image.save(output, format='PNG', optimize=True)
    encoded = base64.b64encode(output.getvalue()).decode('ascii')
    return {'data': [{'b64_json': encoded}]}


def _required(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value:
        raise ModelCapabilityUnavailableError('app_icon_generation', f'{name.lower()}_not_configured', retryable=False)
    return value


def _image_generation_endpoint() -> str:
    base_url = _required('IMAGE_GENERATION_OPENAI_COMPATIBLE_BASE_URL')
    parsed = urlparse(base_url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ModelCapabilityUnavailableError('app_icon_generation', 'invalid_compatible_endpoint', retryable=False)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelCapabilityUnavailableError('app_icon_generation', 'invalid_compatible_endpoint', retryable=False)
    return f"{base_url.rstrip('/')}/images/generations"


def generate_image_via_openai_compatible(
    *,
    prompt: str,
    size: str,
    quality: str,
    n: int,
    timeout_seconds: float = 120.0,
) -> Mapping[str, object]:
    """Call only the explicitly configured image endpoint."""

    endpoint = _image_generation_endpoint()
    api_key = _required('IMAGE_GENERATION_OPENAI_COMPATIBLE_API_KEY')
    model = _required('IMAGE_GENERATION_OPENAI_COMPATIBLE_MODEL')
    circuit_breaker = get_webhook_circuit_breaker(endpoint)
    if not circuit_breaker.allow_request():
        raise ModelCapabilityUnavailableError('app_icon_generation', 'transport_circuit_open', retryable=True)
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(
                endpoint,
                headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                json={
                    'model': model,
                    'prompt': prompt,
                    'size': size,
                    'quality': quality,
                    'n': n,
                    'response_format': 'b64_json',
                },
            )
            response.raise_for_status()
            body = response.json()
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        circuit_breaker.record_failure()
        raise ModelCapabilityUnavailableError('app_icon_generation', 'transport_unavailable', retryable=True) from exc
    except httpx.HTTPStatusError as exc:
        circuit_breaker.record_failure()
        retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
        raise ModelCapabilityUnavailableError(
            'app_icon_generation', 'provider_http_error', retryable=retryable
        ) from exc
    except ValueError as exc:
        circuit_breaker.record_failure()
        raise ModelCapabilityUnavailableError(
            'app_icon_generation', 'provider_invalid_response', retryable=False
        ) from exc
    if not isinstance(body, Mapping):
        circuit_breaker.record_failure()
        raise ModelCapabilityUnavailableError('app_icon_generation', 'provider_invalid_response', retryable=False)
    circuit_breaker.record_success()
    return cast('Mapping[str, object]', body)
