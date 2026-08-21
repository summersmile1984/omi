from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException

from routers.firmware import get_stable_version
from utils.firmware_releases import FirmwareReleaseUnavailable, get_configured_firmware_releases


class _Semaphore:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return None


class _Circuit:
    def __init__(self) -> None:
        self.successes = 0
        self.failures = 0

    def allow_request(self) -> bool:
        return True

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self) -> None:
        self.failures += 1


def _manifest_release(asset_url: str) -> dict[str, object]:
    return {
        'tag_name': 'Omi_CV1_v3.0.15',
        'body': '<!-- KEY_VALUE_START\nrelease_firmware_version: 3.0.15\nKEY_VALUE_END -->',
        'published_at': '2026-08-01T00:00:00Z',
        'draft': False,
        'prerelease': False,
        'assets': [{'name': 'Omi_CV1_OTA_v3.0.15.zip', 'browser_download_url': asset_url}],
    }


def _manifest_env(monkeypatch) -> None:
    monkeypatch.setenv('FIRMWARE_RELEASE_TRANSPORT', 'manifest')
    monkeypatch.setenv('FIRMWARE_RELEASE_MANIFEST_URL', 'https://objects.operator.test/firmware/releases.json')
    monkeypatch.setenv('FIRMWARE_RELEASE_ASSET_ORIGIN', 'https://objects.operator.test')


@pytest.mark.asyncio
async def test_operator_manifest_is_the_only_selected_release_authority(monkeypatch):
    import utils.firmware_releases as mod

    _manifest_env(monkeypatch)
    captured = {}
    cache = {}
    circuit = _Circuit()

    class _Client:
        async def get(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return httpx.Response(
                200,
                json=[_manifest_release('https://objects.operator.test/firmware/ota.zip')],
                request=httpx.Request('GET', url),
            )

    async def fake_run_blocking(_executor, fn, *args, **kwargs):
        if fn is mod.get_generic_cache:
            return cache.get(args[0])
        if fn is mod.set_generic_cache:
            cache[args[0]] = args[1]
            return None
        return fn(*args, **kwargs)

    monkeypatch.setattr(mod, 'run_blocking', fake_run_blocking)
    monkeypatch.setattr(mod, 'get_web_fetch_client', lambda: _Client())
    monkeypatch.setattr(mod, 'get_webhook_semaphore', lambda: _Semaphore())
    monkeypatch.setattr(mod, 'get_webhook_circuit_breaker', lambda _url: circuit)
    monkeypatch.setattr(
        mod,
        'get_omi_github_releases',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('manifest mode must not call GitHub')),
    )

    releases = await get_configured_firmware_releases('firmware-test')

    assert releases[0]['tag_name'] == 'Omi_CV1_v3.0.15'
    assert captured['url'] == 'https://objects.operator.test/firmware/releases.json'
    assert captured['headers'] == {'Accept': 'application/json'}
    assert circuit.successes == 1
    assert circuit.failures == 0


@pytest.mark.asyncio
async def test_manifest_rejects_firmware_asset_on_an_unsigned_origin(monkeypatch):
    import utils.firmware_releases as mod

    _manifest_env(monkeypatch)
    circuit = _Circuit()

    class _Client:
        async def get(self, url, **_kwargs):
            return httpx.Response(
                200,
                json=[_manifest_release('https://github.com/BasedHardware/omi/releases/download/fw.zip')],
                request=httpx.Request('GET', url),
            )

    async def fake_run_blocking(_executor, fn, *_args, **_kwargs):
        if fn is mod.get_generic_cache:
            return None
        raise AssertionError('invalid manifests must not be cached')

    monkeypatch.setattr(mod, 'run_blocking', fake_run_blocking)
    monkeypatch.setattr(mod, 'get_web_fetch_client', lambda: _Client())
    monkeypatch.setattr(mod, 'get_webhook_semaphore', lambda: _Semaphore())
    monkeypatch.setattr(mod, 'get_webhook_circuit_breaker', lambda _url: circuit)

    with pytest.raises(FirmwareReleaseUnavailable, match='firmware_asset_origin_mismatch'):
        await get_configured_firmware_releases('firmware-test')

    assert circuit.failures == 1


@pytest.mark.asyncio
async def test_manifest_missing_url_fails_before_client_or_github(monkeypatch):
    import utils.firmware_releases as mod

    monkeypatch.setenv('FIRMWARE_RELEASE_TRANSPORT', 'manifest')
    monkeypatch.delenv('FIRMWARE_RELEASE_MANIFEST_URL', raising=False)
    monkeypatch.setattr(mod, 'get_web_fetch_client', lambda: (_ for _ in ()).throw(AssertionError('no client')))
    monkeypatch.setattr(
        mod,
        'get_omi_github_releases',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('no GitHub')),
    )

    with pytest.raises(FirmwareReleaseUnavailable, match='manifest_url_not_configured'):
        await get_configured_firmware_releases('firmware-test')


@pytest.mark.asyncio
async def test_disabled_firmware_transport_is_a_typed_route_response(monkeypatch):
    monkeypatch.setenv('FIRMWARE_RELEASE_TRANSPORT', 'disabled')

    with pytest.raises(HTTPException) as exc_info:
        await get_stable_version(device_model='Omi CV 1')

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        'code': 'deployment_capability_unavailable',
        'capability': 'firmware_updates',
        'reason': 'firmware_updates_disabled',
        'retryable': False,
    }
