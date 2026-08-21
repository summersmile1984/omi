"""Deployment-selected firmware release catalog boundary.

Cloud deployments retain the historical GitHub transport. Self-hosted
deployments can instead point at an operator-owned JSON manifest with the same
release shape. The manifest and every firmware asset are pinned to explicit
origins; there is no GitHub/Omi fallback after selecting ``manifest``.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any, Optional, cast
from urllib.parse import urlparse

import httpx

from database.redis_db import get_generic_cache, set_generic_cache
from utils.executors import db_executor, run_blocking
from utils.github_releases import get_omi_github_releases
from utils.http_client import get_web_fetch_client, get_webhook_circuit_breaker, get_webhook_semaphore

_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_RELEASES = 1000


class FirmwareReleaseUnavailable(RuntimeError):
    def __init__(self, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable

    def as_dict(self) -> dict[str, object]:
        return {
            'code': 'deployment_capability_unavailable',
            'capability': 'firmware_updates',
            'reason': self.reason,
            'retryable': self.retryable,
        }


def firmware_release_transport() -> str:
    configured = os.getenv('FIRMWARE_RELEASE_TRANSPORT', '').strip().lower()
    if configured:
        transport = configured
    else:
        # Managed deployments preserve the historical GitHub catalog. A
        # neutral/self-hosted process must not reach that catalog merely
        # because it was launched outside the reviewed Compose overlay (or
        # inherited a stale environment without the explicit binding).
        profile = os.getenv('OMI_DEPLOYMENT_PROFILE', '').strip().lower()
        transport = 'disabled' if profile in {'neutral', 'self_hosted', 'self-hosted'} else 'github'
    if transport not in {'github', 'manifest', 'disabled'}:
        raise FirmwareReleaseUnavailable('unsupported_firmware_release_transport', retryable=False)
    return transport


def _validated_origin(value: str, *, setting: str) -> str:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {'', '/'}
    ):
        raise FirmwareReleaseUnavailable(f'invalid_{setting.lower()}', retryable=False)
    host = parsed.hostname.lower()
    default_port = (parsed.scheme == 'https' and parsed.port in {None, 443}) or (
        parsed.scheme == 'http' and parsed.port in {None, 80}
    )
    authority = host if default_port else f'{host}:{parsed.port}'
    return f'{parsed.scheme}://{authority}'


def _manifest_config() -> tuple[str, str, str | None]:
    manifest_url = os.getenv('FIRMWARE_RELEASE_MANIFEST_URL', '').strip()
    if not manifest_url:
        raise FirmwareReleaseUnavailable('firmware_release_manifest_url_not_configured', retryable=False)
    parsed = urlparse(manifest_url)
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise FirmwareReleaseUnavailable('invalid_firmware_release_manifest_url', retryable=False)
    asset_origin = _validated_origin(
        os.getenv('FIRMWARE_RELEASE_ASSET_ORIGIN', ''), setting='FIRMWARE_RELEASE_ASSET_ORIGIN'
    )
    token = os.getenv('FIRMWARE_RELEASE_MANIFEST_BEARER_TOKEN', '').strip() or None
    return manifest_url, asset_origin, token


def _asset_matches_origin(url: object, expected_origin: str) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return False
    try:
        return _validated_origin(f'{parsed.scheme}://{parsed.netloc}', setting='firmware_asset_url') == expected_origin
    except (FirmwareReleaseUnavailable, ValueError):
        return False


def _validate_manifest(
    body: object, *, asset_origin: str, tag_filter: Optional[re.Pattern[str]]
) -> list[dict[str, Any]]:
    if not isinstance(body, list) or len(body) > _MAX_RELEASES:
        raise FirmwareReleaseUnavailable('invalid_firmware_release_manifest', retryable=False)

    releases: list[dict[str, Any]] = []
    for raw_release in body:
        if not isinstance(raw_release, Mapping):
            raise FirmwareReleaseUnavailable('invalid_firmware_release_manifest', retryable=False)
        release = dict(cast(Mapping[str, Any], raw_release))
        tag_name = release.get('tag_name')
        assets = release.get('assets')
        if not isinstance(tag_name, str) or not isinstance(assets, list):
            raise FirmwareReleaseUnavailable('invalid_firmware_release_manifest', retryable=False)
        for raw_asset in assets:
            if not isinstance(raw_asset, Mapping):
                raise FirmwareReleaseUnavailable('invalid_firmware_release_manifest', retryable=False)
            if not _asset_matches_origin(raw_asset.get('browser_download_url'), asset_origin):
                raise FirmwareReleaseUnavailable('firmware_asset_origin_mismatch', retryable=False)
        if tag_filter is None or tag_filter.match(tag_name):
            releases.append(release)
    return releases


async def _get_manifest_releases(cache_key: str, tag_filter: Optional[re.Pattern[str]]) -> list[dict[str, Any]]:
    manifest_url, asset_origin, token = _manifest_config()
    identity = hashlib.sha256(f'{manifest_url}\0{asset_origin}'.encode()).hexdigest()[:24]
    effective_cache_key = f'{cache_key}:operator-manifest:{identity}'
    cached = await run_blocking(db_executor, get_generic_cache, effective_cache_key)
    if isinstance(cached, list):
        return cast(list[dict[str, Any]], cached)

    circuit_breaker = get_webhook_circuit_breaker(manifest_url)
    if not circuit_breaker.allow_request():
        raise FirmwareReleaseUnavailable('firmware_manifest_circuit_open', retryable=True)
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        async with get_webhook_semaphore():
            response = await get_web_fetch_client().get(manifest_url, headers=headers, timeout=20.0)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        circuit_breaker.record_failure()
        raise FirmwareReleaseUnavailable('firmware_manifest_transport_unavailable', retryable=True) from exc

    if response.status_code >= 400:
        circuit_breaker.record_failure()
        retryable = response.status_code == 429 or response.status_code >= 500
        raise FirmwareReleaseUnavailable('firmware_manifest_http_error', retryable=retryable)
    if len(response.content) > _MAX_MANIFEST_BYTES:
        circuit_breaker.record_failure()
        raise FirmwareReleaseUnavailable('firmware_release_manifest_too_large', retryable=False)
    try:
        releases = _validate_manifest(response.json(), asset_origin=asset_origin, tag_filter=tag_filter)
    except (ValueError, FirmwareReleaseUnavailable) as exc:
        circuit_breaker.record_failure()
        if isinstance(exc, FirmwareReleaseUnavailable):
            raise
        raise FirmwareReleaseUnavailable('invalid_firmware_release_manifest', retryable=False) from exc

    circuit_breaker.record_success()
    await run_blocking(db_executor, set_generic_cache, effective_cache_key, releases, ttl=300)
    return releases


async def get_configured_firmware_releases(
    cache_key: str, tag_filter: Optional[re.Pattern[str]] = None
) -> list[dict[str, Any]]:
    """Return firmware releases from the deployment-selected authority."""

    transport = firmware_release_transport()
    if transport == 'disabled':
        raise FirmwareReleaseUnavailable('firmware_updates_disabled', retryable=False)
    if transport == 'manifest':
        return await _get_manifest_releases(cache_key, tag_filter)
    return (await get_omi_github_releases(cache_key, tag_filter=tag_filter)) or []
