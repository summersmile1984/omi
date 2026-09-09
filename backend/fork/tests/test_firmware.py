"""Server OS firmware policy tests through the real router seam."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from fork import firmware
from fork.registry import build_registry


def _config(brand: str = "weft") -> dict:
    return {
        "schema_version": 1,
        "brand_id": brand,
        "build": {
            "ble_name": "Weft",
            "ble_name_devkit": "Weft DevKit",
            "dis_manufacturer": "Weft Hardware",
            "dis_model_cv1": "Weft CV1",
            "nfc_pair_url": "https://pair.weft.invalid/p?id=%s",
            "service_uuid_base": "",
            "mcuboot_signing_key": "env:WEFT_MCUBOOT_KEY",
        },
        "release": {
            "schema_version": 1,
            "brand_id": brand,
            "device_model": "Weft CV1",
            "device_model_aliases": ["nrf5340"],
            "release_tag_prefix": "Weft_CV1_v",
            "release_asset_prefix": "Weft_CV1_OTA_v",
            "github_releases_url": "https://api.github.com/repos/weft/firmware/releases",
        },
    }


def _write_config(directory: Path) -> Path:
    path = directory / "firmware.json"
    path.write_text(json.dumps(_config()))
    return path


def test_policy_loads_only_a_matching_generated_brand_document():
    with tempfile.TemporaryDirectory() as directory:
        policy = firmware.load_policy(_write_config(Path(directory)))
    assert policy.brand_id == "weft"
    assert policy.supports_model("Weft CV1")
    assert policy.supports_model("nrf5340")
    assert not policy.supports_model("Omi CV 1")
    assert policy.tag_pattern.fullmatch("Weft_CV1_v3.1.0")
    assert not policy.tag_pattern.fullmatch("Omi_CV1_v3.1.0")


def test_operator_manifest_is_the_only_server_os_release_source():
    class Response:
        status_code = 200

        def json(self):
            return [
                {"tag_name": "Weft_CV1_v3.1.0"},
                {"tag_name": "Omi_CV1_v3.1.0"},
            ]

    class Client:
        async def get(self, url, *, headers):
            assert url == "https://objects.weft.invalid/firmware/releases.json"
            assert headers == {"accept": "application/json", "authorization": "Bearer fixture-token"}
            return Response()

    with tempfile.TemporaryDirectory() as directory:
        policy = firmware.load_policy(_write_config(Path(directory)))
    with mock.patch.dict(
        os.environ,
        {
            "FIRMWARE_RELEASE_TRANSPORT": "manifest",
            "FIRMWARE_RELEASE_MANIFEST_URL": "https://objects.weft.invalid/firmware/releases.json",
            "FIRMWARE_RELEASE_MANIFEST_BEARER_TOKEN": "fixture-token",
        },
        clear=False,
    ):
        releases = asyncio.run(firmware.operator_manifest_releases(policy, policy.tag_pattern, client_factory=Client))
    assert releases == [{"tag_name": "Weft_CV1_v3.1.0"}]


def test_real_router_seam_uses_custom_model_and_tag_without_upstream_aliases():
    import routers.firmware as router

    profile = {"name": "self_hosted.local", "target": "self_hosted", "data_plane": {"object_store": "minio"}}
    with tempfile.TemporaryDirectory() as directory:
        config_path = _write_config(Path(directory))
        originals = [(patch.target()[0], patch.attribute, patch.target()[1]) for patch in firmware.patches()]
        try:
            with mock.patch.object(firmware, "CONFIG_PATH", config_path):
                build_registry(firmware.patches()).apply(profile)
                device = router._get_device_by_model_number("Weft CV1")
                assert device is not None
                assert router._get_device_by_model_number("Omi CV 1") is None
                assert router._get_release_prefix(device) == "Weft_CV1"
                candidates = router._find_candidate_releases(
                    [
                        {
                            "tag_name": "Weft_CV1_v3.1.0",
                            "published_at": "2026-09-05T00:00:00Z",
                            "body": "<!-- KEY_VALUE_START\nrelease_firmware_version: 3.1.0\nKEY_VALUE_END -->",
                        },
                        {
                            "tag_name": "Omi_CV1_v9.9.9",
                            "published_at": "2026-09-05T00:00:00Z",
                            "body": "<!-- KEY_VALUE_START\nrelease_firmware_version: 9.9.9\nKEY_VALUE_END -->",
                        },
                    ],
                    router._get_release_prefix(device),
                )
                assert [candidate["tag_name"] for candidate in candidates] == ["Weft_CV1_v3.1.0"]
        finally:
            for module, attribute, original in originals:
                setattr(module, attribute, original)
