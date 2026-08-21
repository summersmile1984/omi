from __future__ import annotations

import base64
from io import BytesIO

import httpx
from PIL import Image, ImageChops
import pytest

from utils.llm.capabilities import ModelCapabilityUnavailableError


class _Circuit:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.successes = 0
        self.failures = 0

    def allow_request(self) -> bool:
        return self.allowed

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self) -> None:
        self.failures += 1


def _env(monkeypatch) -> None:
    monkeypatch.setenv('IMAGE_GENERATION_OPENAI_COMPATIBLE_BASE_URL', 'http://image.internal/v1')
    monkeypatch.setenv('IMAGE_GENERATION_OPENAI_COMPATIBLE_API_KEY', 'operator-key')
    monkeypatch.setenv('IMAGE_GENERATION_OPENAI_COMPATIBLE_MODEL', 'local-image-model')


def test_local_template_is_deterministic_grayscale_png_without_network(monkeypatch):
    import utils.llm.image_generation_provider as mod

    monkeypatch.setattr(mod.httpx, 'Client', lambda **_kwargs: (_ for _ in ()).throw(AssertionError('no client')))

    first = mod.generate_image_via_local_template(prompt='neutral operator app', size='1024x1024')
    second = mod.generate_image_via_local_template(prompt='neutral operator app', size='1024x1024')

    assert first == second
    encoded = first['data'][0]['b64_json']
    image = Image.open(BytesIO(base64.b64decode(encoded, validate=True)))
    assert image.format == 'PNG'
    assert image.size == (1024, 1024)
    red, green, blue = image.split()
    assert ImageChops.difference(red, green).getbbox() is None
    assert ImageChops.difference(green, blue).getbbox() is None


def test_compatible_image_provider_posts_only_to_explicit_endpoint(monkeypatch):
    import utils.llm.image_generation_provider as mod

    _env(monkeypatch)
    captured = {}
    circuit = _Circuit()

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {'data': [{'b64_json': 'aWNvbg=='}]}

    class _Client:
        def __init__(self, **kwargs):
            captured['client'] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return None

        def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return _Response()

    monkeypatch.setattr(mod.httpx, 'Client', _Client)
    monkeypatch.setattr(mod, 'get_webhook_circuit_breaker', lambda _url: circuit)

    result = mod.generate_image_via_openai_compatible(prompt='draw it', size='1024x1024', quality='medium', n=1)

    assert result == {'data': [{'b64_json': 'aWNvbg=='}]}
    assert captured['url'] == 'http://image.internal/v1/images/generations'
    assert captured['headers']['Authorization'] == 'Bearer operator-key'
    assert captured['json'] == {
        'model': 'local-image-model',
        'prompt': 'draw it',
        'size': '1024x1024',
        'quality': 'medium',
        'n': 1,
        'response_format': 'b64_json',
    }
    assert circuit.successes == 1
    assert circuit.failures == 0


def test_compatible_image_provider_missing_key_fails_before_client_construction(monkeypatch):
    import utils.llm.image_generation_provider as mod

    _env(monkeypatch)
    monkeypatch.delenv('IMAGE_GENERATION_OPENAI_COMPATIBLE_API_KEY', raising=False)
    monkeypatch.setattr(mod.httpx, 'Client', lambda **_kwargs: (_ for _ in ()).throw(AssertionError('no client')))
    monkeypatch.setattr(
        mod, 'get_webhook_circuit_breaker', lambda _url: (_ for _ in ()).throw(AssertionError('no circuit'))
    )

    with pytest.raises(ModelCapabilityUnavailableError) as error:
        mod.generate_image_via_openai_compatible(prompt='draw it', size='1024x1024', quality='medium', n=1)

    assert error.value.as_dict()['reason'] == 'image_generation_openai_compatible_api_key_not_configured'


def test_compatible_image_provider_maps_retryable_http_failures(monkeypatch):
    import utils.llm.image_generation_provider as mod

    _env(monkeypatch)
    circuit = _Circuit()

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return None

        def post(self, url, **_kwargs):
            request = httpx.Request('POST', url)
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError('unavailable', request=request, response=response)

    monkeypatch.setattr(mod.httpx, 'Client', _Client)
    monkeypatch.setattr(mod, 'get_webhook_circuit_breaker', lambda _url: circuit)

    with pytest.raises(ModelCapabilityUnavailableError) as error:
        mod.generate_image_via_openai_compatible(prompt='draw it', size='1024x1024', quality='medium', n=1)

    assert error.value.as_dict()['reason'] == 'provider_http_error'
    assert error.value.as_dict()['retryable'] is True
    assert circuit.failures == 1


def test_compatible_image_provider_open_circuit_never_constructs_client(monkeypatch):
    import utils.llm.image_generation_provider as mod

    _env(monkeypatch)
    circuit = _Circuit(allowed=False)
    monkeypatch.setattr(mod, 'get_webhook_circuit_breaker', lambda _url: circuit)
    monkeypatch.setattr(mod.httpx, 'Client', lambda **_kwargs: (_ for _ in ()).throw(AssertionError('no client')))

    with pytest.raises(ModelCapabilityUnavailableError) as error:
        mod.generate_image_via_openai_compatible(prompt='draw it', size='1024x1024', quality='medium', n=1)

    assert error.value.as_dict() == {
        'code': 'model_capability_unavailable',
        'capability': 'app_icon_generation',
        'reason': 'transport_circuit_open',
        'retryable': True,
    }


@pytest.mark.asyncio
async def test_app_generator_rejects_base64_that_is_not_a_real_icon(monkeypatch):
    import utils.llm.app_generator as app_generator

    monkeypatch.setenv('APP_ICON_GENERATION_TRANSPORT', 'openai_compatible')
    _env(monkeypatch)
    monkeypatch.setattr(
        app_generator,
        'generate_image_via_openai_compatible',
        lambda **_kwargs: {'data': [{'b64_json': base64.b64encode(b'not-an-image').decode('ascii')}]},
    )

    with pytest.raises(ModelCapabilityUnavailableError) as raised:
        await app_generator.generate_app_icon('Local', 'Invalid provider payload', 'other')

    assert raised.value.as_dict()['reason'] == 'provider_invalid_image'
