import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import entry  # noqa: E402

from entry import DEVICE_PREFIXES, _asset_key, _firmware_metadata, _firmware_response  # noqa: E402


def test_asset_keys_are_uid_scoped_and_reject_traversal():
    assert _asset_key("user-1", "audio/clip.wav") == "user-1/audio/clip.wav"
    assert _asset_key("user-1", "../other-user/clip.wav") is None
    assert _asset_key("user-1", "") is None


def test_firmware_metadata_preserves_release_contract():
    body = """<!-- KEY_VALUE_START
release_firmware_version: 3.0.21
ota_update_steps: erase, flash
changelog: Fix audio | Improve battery
KEY_VALUE_END -->"""
    release = {
        "body": body,
        "assets": [{"name": "Omi_CV1_OTA_v3.0.21.zip", "browser_download_url": "https://example.test/fw.zip"}],
    }
    assert DEVICE_PREFIXES["Omi CV 1"] == "Omi_CV1"
    assert _firmware_metadata(body)["ota_update_steps"] == ["erase", "flash"]
    assert _firmware_response("Omi_CV1", release)["zip_url"] == "https://example.test/fw.zip"


def test_firmware_route_uses_worker_fetch(monkeypatch):
    release = {
        "tag_name": "Omi_CV1_v3.0.21",
        "published_at": "2026-08-27T00:00:00Z",
        "body": "<!-- KEY_VALUE_START\nrelease_firmware_version: 3.0.21\nKEY_VALUE_END -->",
        "assets": [{"name": "Omi_CV1_OTA_v3.0.21.zip", "browser_download_url": "https://example.test/fw.zip"}],
    }

    class FakeResponse:
        status = 200

        async def json(self):
            return [release]

    calls = {}

    async def fake_fetch(url, **options):
        calls["url"] = url
        calls["options"] = options
        return FakeResponse()

    monkeypatch.setattr(entry, "worker_fetch", fake_fetch)
    request = type("Request", (), {"scope": {"env": type("Env", (), {})()}})()

    result = asyncio.run(entry.firmware_stable("Omi CV 1", request))

    assert calls["url"].endswith("/releases?per_page=100")
    assert calls["options"]["headers"]["user-agent"] == "omi-cloudflare-worker/0.1"
    assert result["version"] == "3.0.21"


def test_firmware_latest_and_version_routes_share_release_adapter(monkeypatch):
    release = {
        "tag_name": "Omi_CV1_v3.0.21",
        "published_at": "2026-08-27T00:00:00Z",
        "body": (
            "<!-- KEY_VALUE_START\nrelease_firmware_version: 3.0.21\n"
            "minimum_firmware_required: 3.0.6\nKEY_VALUE_END -->"
        ),
        "assets": [{"name": "Omi_CV1_OTA_v3.0.21.zip", "browser_download_url": "https://example.test/fw.zip"}],
    }

    class FakeResponse:
        status = 200

        async def json(self):
            return [release]

    async def fake_fetch(url, **options):
        return FakeResponse()

    monkeypatch.setattr(entry, "worker_fetch", fake_fetch)
    request = type("Request", (), {"scope": {"env": type("Env", (), {})()}})()

    latest = asyncio.run(entry.firmware_latest("Omi CV 1", "3.0.6", "", "", request))
    exact = asyncio.run(entry.firmware_version("Omi CV 1", "v3.0.21", request))

    assert latest["version"] == "3.0.21"
    assert exact["zip_url"] == "https://example.test/fw.zip"
