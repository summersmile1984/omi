"""Behavioral tests for the shared firmware manifest validator.

The validator (``scripts/brand/generators/firmware_validate.py``) is the
single source of truth for every regex / structural check the fork's firmware
pipeline applies. Both the producer (``scripts/brand/generators/firmware.py``)
and the stage consumer (``omi/firmware/fork/stage.py``) call it, so any
relaxation here would relax both sides simultaneously. These tests pin the
constraints so the convergence cannot silently drift.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BRAND_SCRIPTS = Path(__file__).resolve().parent
GENERATORS = BRAND_SCRIPTS / "generators"
sys.path.insert(0, str(GENERATORS))

import firmware_validate  # noqa: E402


def _good_config() -> dict:
    return {
        "schema_version": 1,
        "brand_id": "weft-fixture",
        "build": {
            "ble_name": "Weft",
            "ble_name_devkit": "Weft DevKit",
            "dis_manufacturer": "Weft Hardware",
            "dis_model_cv1": "Weft CV1",
            "nfc_pair_url": "https://pair.weft.invalid/p?id=%s",
            "service_uuid_base": "0000aaaa-0000-1000-8000-00805f9b34fb",
            "mcuboot_signing_key": "env:WEFT_MCUBOOT_KEY",
        },
        "release": {
            "schema_version": 1,
            "brand_id": "weft-fixture",
            "device_model": "Weft CV1",
            "device_model_aliases": ["nrf5340"],
            "release_tag_prefix": "Weft_CV1_v",
            "release_asset_prefix": "Weft_CV1_OTA_v",
            "github_releases_url": "https://api.github.com/repos/weft/firmware/releases",
        },
    }


def _good_manifest() -> dict:
    return {
        "brand": {"id": "weft-fixture", "display_name": "Weft"},
        "device": {
            "ble_name": "Weft",
            "dis_model_cv1": "Weft CV1",
            "firmware_release_prefix": "Weft_CV1_v",
            "nfc_pair_url": "https://pair.weft.invalid/p?id=%s",
        },
        "distribution": {"github_releases_repo": "weft/firmware"},
    }


class ValidateGeneratedConfigTests(unittest.TestCase):
    def test_a_well_formed_config_passes_strict_and_loose(self):
        config = _good_config()
        self.assertEqual(firmware_validate.validate_generated_config(config, strict=True), config)
        self.assertEqual(firmware_validate.validate_generated_config(config, strict=False), config)

    def test_nfc_url_with_two_placeholders_is_rejected(self):
        config = _good_config()
        config["build"]["nfc_pair_url"] = "https://pair.weft.invalid/?a=%s&b=%s"
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "placeholder"):
            firmware_validate.validate_generated_config(config, strict=True)

    def test_nfc_url_without_https_scheme_is_rejected(self):
        config = _good_config()
        config["build"]["nfc_pair_url"] = "http://pair.weft.invalid/?id=%s"
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "HTTPS"):
            firmware_validate.validate_generated_config(config, strict=True)

    def test_nfc_url_with_credentials_is_rejected(self):
        config = _good_config()
        config["build"]["nfc_pair_url"] = "https://user:pw@pair.weft.invalid/?id=%s"
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "credentials"):
            firmware_validate.validate_generated_config(config, strict=True)

    def test_nfc_url_exceeding_the_64_byte_buffer_is_rejected(self):
        config = _good_config()
        # 60 chars + ?id=%s = 65 ASCII bytes when %s is replaced with ABC123 (6 bytes).
        config["build"]["nfc_pair_url"] = "https://pair.weft.invalid/" + "x" * 60 + "?id=%s"
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "NFC URI buffer"):
            firmware_validate.validate_generated_config(config, strict=True)

    def test_build_model_must_equal_release_device_model(self):
        config = _good_config()
        config["release"]["device_model"] = "Different Model"
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "model"):
            firmware_validate.validate_generated_config(config, strict=True)

    def test_release_policy_brand_must_match_top_level_brand(self):
        config = _good_config()
        config["release"]["brand_id"] = "other-brand"
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "another brand"):
            firmware_validate.validate_generated_config(config, strict=True)

    def test_release_tag_prefix_must_end_in_v(self):
        config = _good_config()
        config["release"]["release_tag_prefix"] = "Weft_CV1"
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "_v"):
            firmware_validate.validate_generated_config(config, strict=True)

    def test_release_asset_prefix_must_be_tag_without_v_plus_ota_v(self):
        config = _good_config()
        config["release"]["release_asset_prefix"] = "Weft_CV1_DIFF_OTA_v"
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "naming policy"):
            firmware_validate.validate_generated_config(config, strict=True)

    def test_github_releases_url_must_point_to_the_api(self):
        config = _good_config()
        config["release"]["github_releases_url"] = "https://example.com/repos/weft/firmware/releases"
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "GitHub Releases"):
            firmware_validate.validate_generated_config(config, strict=True)

    def test_strict_mode_rejects_kconfig_unsafe_dis_model(self):
        config = _good_config()
        # DEL (0x7F) is in the strict regex's exclusion range but is *not*
        # covered by the historical stage.py ``ord(char) < 32`` check.
        # We mirror the bad value into release.device_model so the cross-
        # field equality check passes; the only error that should surface
        # is the kconfig one.
        config["build"]["dis_model_cv1"] = "Weft\x7fCV1"
        config["release"]["device_model"] = "Weft\x7fCV1"
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "Kconfig-safe"):
            firmware_validate.validate_generated_config(config, strict=True)

    def test_loose_mode_accepts_what_stage_py_historically_accepted(self):
        # Stage.py historically checked ``ord(char) < 32 or char in '\\"'``.
        # A DEL character (0x7F) is rejected by the strict regex (which
        # excludes \\x00-\\x1f and \\x7f) but is *not* rejected by the
        # historical stage.py check (which only excludes chars below 32).
        # The shared validator's ``strict=False`` mode preserves that exact
        # backwards-compatible behaviour.
        config = _good_config()
        config["build"]["dis_model_cv1"] = "Weft\x7fCV1"
        config["release"]["device_model"] = "Weft\x7fCV1"
        self.assertEqual(
            firmware_validate.validate_generated_config(config, strict=False), config
        )
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "Kconfig-safe"):
            firmware_validate.validate_generated_config(config, strict=True)

    def test_top_level_extra_keys_are_rejected(self):
        config = _good_config()
        config["unexpected"] = 1
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "exactly:"):
            firmware_validate.validate_generated_config(config, strict=True)

    def test_schema_version_must_be_one(self):
        config = _good_config()
        config["schema_version"] = 2
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "identity"):
            firmware_validate.validate_generated_config(config, strict=True)


class ValidateBrandManifestSourceTests(unittest.TestCase):
    def test_a_well_formed_manifest_passes(self):
        # ``validate_brand_manifest_source`` only inspects the three
        # firmware-relevant fields of the brand manifest; other manifest
        # fields are out of scope for this validator.
        firmware_validate.validate_brand_manifest_source(_good_manifest())

    def test_missing_release_prefix_is_rejected(self):
        manifest = _good_manifest()
        manifest["device"].pop("firmware_release_prefix")
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "firmware_release_prefix"):
            firmware_validate.validate_brand_manifest_source(manifest)

    def test_invalid_github_repo_owner_format_is_rejected(self):
        manifest = _good_manifest()
        manifest["distribution"]["github_releases_repo"] = "weft firmware with spaces"
        with self.assertRaisesRegex(firmware_validate.FirmwareValidationError, "owner/repository"):
            firmware_validate.validate_brand_manifest_source(manifest)


class SharedConstantsTests(unittest.TestCase):
    def test_top_keys_match_what_producer_and_stage_have_always_emitted(self):
        # The exact-key sets are part of the public contract between the
        # producer (which writes the config) and the stage consumer (which
        # re-reads it); changing them is a breaking change for any already-
        # generated config checked into the tree.
        self.assertEqual(
            firmware_validate.TOP_KEYS,
            frozenset({"schema_version", "brand_id", "build", "release"}),
        )
        self.assertEqual(len(firmware_validate.BUILD_KEYS), 7)
        self.assertEqual(len(firmware_validate.RELEASE_KEYS), 7)
        self.assertEqual(firmware_validate.SCHEMA_VERSION, 1)
        self.assertEqual(firmware_validate.MODEL_ALIASES, ("nrf5340",))


if __name__ == "__main__":
    unittest.main()
