"""Validated, brand-scoped firmware policy for the API Core Worker.

Every Worker bundle receives this public policy from the same brand manifest
that produced the CV1 staging input. There is intentionally no built-in Omi
default: a missing policy must fail before the release source is contacted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_BRAND_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PREFIX = re.compile(r"^[A-Za-z0-9_]+_v$")
_VERSION = r"[0-9]+(?:\.[0-9]+){1,2}"
_POLICY_KEYS = {
    "schema_version",
    "brand_id",
    "device_model",
    "device_model_aliases",
    "release_tag_prefix",
    "release_asset_prefix",
    "github_releases_url",
}


class FirmwarePolicyError(ValueError):
    pass


@dataclass(frozen=True)
class FirmwarePolicy:
    brand_id: str
    device_model: str
    device_model_aliases: tuple[str, ...]
    release_tag_prefix: str
    release_asset_prefix: str
    github_releases_url: str

    @property
    def tag_pattern(self) -> re.Pattern[str]:
        return re.compile(rf"^{re.escape(self.release_tag_prefix)}{_VERSION}$", re.IGNORECASE)

    def supports_model(self, model: str) -> bool:
        return model == self.device_model or model in self.device_model_aliases

    def has_release_tag(self, tag: object) -> bool:
        return isinstance(tag, str) and bool(self.tag_pattern.fullmatch(tag))

    def has_ota_asset(self, name: object) -> bool:
        return (
            isinstance(name, str)
            and name.lower().startswith(self.release_asset_prefix.lower())
            and name.lower().endswith(".zip")
        )


def _plain(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise FirmwarePolicyError(f"{label} must be a non-empty plain string")
    return value


def from_json(raw: object) -> FirmwarePolicy:
    if not isinstance(raw, str) or not raw or len(raw) > 8_192:
        raise FirmwarePolicyError("FIRMWARE_BRAND_POLICY_JSON is required")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise FirmwarePolicyError("FIRMWARE_BRAND_POLICY_JSON is not valid JSON") from error
    if not isinstance(value, dict) or set(value) != _POLICY_KEYS:
        raise FirmwarePolicyError("firmware policy has an unexpected shape")
    if value["schema_version"] != 1:
        raise FirmwarePolicyError("unsupported firmware policy schema")
    brand_id = _plain(value["brand_id"], "brand_id")
    if not _BRAND_ID.fullmatch(brand_id):
        raise FirmwarePolicyError("firmware policy brand_id is invalid")
    device_model = _plain(value["device_model"], "device_model")
    aliases = value["device_model_aliases"]
    if not isinstance(aliases, list) or not aliases or len(aliases) > 8:
        raise FirmwarePolicyError("firmware policy device_model_aliases is invalid")
    aliases = tuple(_plain(alias, "device_model_aliases") for alias in aliases)
    if device_model in aliases or len(set(aliases)) != len(aliases):
        raise FirmwarePolicyError("firmware policy aliases must be distinct")
    tag_prefix = _plain(value["release_tag_prefix"], "release_tag_prefix")
    asset_prefix = _plain(value["release_asset_prefix"], "release_asset_prefix")
    if not _PREFIX.fullmatch(tag_prefix) or asset_prefix != tag_prefix[:-1] + "OTA_v":
        raise FirmwarePolicyError("firmware release naming policy is invalid")
    releases_url = _plain(value["github_releases_url"], "github_releases_url")
    parsed = urlsplit(releases_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise FirmwarePolicyError("firmware release source must be an explicit HTTPS URL")
    return FirmwarePolicy(brand_id, device_model, aliases, tag_prefix, asset_prefix, releases_url.rstrip("/"))


def from_env(env: object) -> FirmwarePolicy:
    return from_json(getattr(env, "FIRMWARE_BRAND_POLICY_JSON", None))
