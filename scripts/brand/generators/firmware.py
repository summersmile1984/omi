"""Render the fork-owned firmware identity and OTA policy input.

The upstream firmware source is deliberately left byte-identical.  The
``omi/firmware/fork/stage.py`` consumer copies that source into an isolated
build directory and applies this generated input there.  Server OS and
Cloudflare consume the smaller public release policy from the same document.

All field validation is delegated to :mod:`firmware_validate` so the producer
and the stage consumer enforce the exact same set of constraints; backend
``backend/fork/firmware.py::load_policy`` is deliberately kept independent
(it cannot import the ``scripts/brand/generators/`` tree from the backend).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import firmware_validate as _validate  # noqa: E402

GENERATED_HEADER = _validate.GENERATED_HEADER
FIRMWARE_CONFIG_PATH = "backend/fork/firmware_brand.generated.json"
MODEL_ALIASES = _validate.MODEL_ALIASES
_GENERATED_HEADER_KEY = "// " + GENERATED_HEADER + "\n"


class FirmwareRenderError(ValueError):
    """A schema-valid manifest cannot safely configure the CV1 build."""


def _safe_kconfig_value(name: str, value: object) -> str:
    # Backwards-compat shim: existing call sites pass strings in from the
    # brand manifest YAML, which ``firmware_validate._safe_kconfig_value``
    # also accepts. The shared validator raises ``FirmwareValidationError``;
    # translate it to the historical ``FirmwareRenderError`` so any caller
    # still matching on the legacy class keeps working.
    try:
        return _validate._safe_kconfig_value(name, value)
    except _validate.FirmwareValidationError as error:
        raise FirmwareRenderError(str(error)) from error


def _release_policy(manifest: dict) -> dict[str, object]:
    device = manifest["device"]
    prefix = device["firmware_release_prefix"]
    try:
        repository = manifest["distribution"]["github_releases_repo"]
    except (KeyError, TypeError) as error:
        raise FirmwareRenderError(
            "distribution.github_releases_repo is required"
        ) from error
    if not isinstance(prefix, str) or not _validate._RELEASE_TAG_PREFIX.fullmatch(prefix):
        raise FirmwareRenderError("device.firmware_release_prefix must end in '_v'")
    if not isinstance(repository, str) or not _validate._GITHUB_REPOSITORY.fullmatch(repository):
        raise FirmwareRenderError("distribution.github_releases_repo must be an owner/repository pair")
    return {
        "schema_version": _validate.SCHEMA_VERSION,
        "brand_id": manifest["brand"]["id"],
        "device_model": _safe_kconfig_value("dis_model_cv1", device["dis_model_cv1"]),
        "device_model_aliases": list(MODEL_ALIASES),
        "release_tag_prefix": prefix,
        "release_asset_prefix": prefix[:-1] + _validate._RELEASE_ASSET_SUFFIX,
        "github_releases_url": f"https://api.github.com/repos/{repository}/releases",
    }


def public_policy(manifest: dict) -> dict[str, object]:
    """Return only the safe OTA lookup policy projected to public runtimes."""

    return _release_policy(manifest)


def generated_config(manifest: dict) -> dict[str, object]:
    """Return the complete, non-secret staging input for one firmware brand."""

    device = manifest["device"]
    config = {
        "schema_version": _validate.SCHEMA_VERSION,
        "brand_id": manifest["brand"]["id"],
        "build": {
            "ble_name": _safe_kconfig_value("ble_name", device["ble_name"]),
            "ble_name_devkit": _safe_kconfig_value("ble_name_devkit", device["ble_name_devkit"]),
            "dis_manufacturer": _safe_kconfig_value("dis_manufacturer", device["dis_manufacturer"]),
            "dis_model_cv1": _safe_kconfig_value("dis_model_cv1", device["dis_model_cv1"]),
            "nfc_pair_url": device["nfc_pair_url"],
            "service_uuid_base": device["service_uuid_base"],
            "mcuboot_signing_key": device["mcuboot_signing_key"],
        },
        "release": _release_policy(manifest),
    }
    try:
        return _validate.validate_generated_config(config, strict=True)
    except _validate.FirmwareValidationError as error:
        raise FirmwareRenderError(str(error)) from error


def render(manifest: dict) -> dict[str, str]:
    config = generated_config(manifest)
    return {
        FIRMWARE_CONFIG_PATH: _GENERATED_HEADER_KEY + json.dumps(config, indent=2, sort_keys=True) + "\n",
    }
