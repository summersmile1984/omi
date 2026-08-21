import os
from typing import Any, Literal, Optional, cast
from urllib.parse import urlparse

from database._client import get_firestore_client

# Managed deployments preserve the historical manual recovery route. Neutral
# deployments must never inherit it: a missing operator release URL is a typed
# disabled capability, not permission to send a client to Omi infrastructure.
DEFAULT_DESKTOP_DOWNLOAD_URL = "https://api.omi.me/v2/desktop/download/latest?channel=stable"
DESKTOP_UPDATE_DOWNLOAD_URL_ENV = "DESKTOP_UPDATE_DOWNLOAD_URL"
NEUTRAL_DEPLOYMENT_PROFILES = frozenset({"neutral", "self_hosted", "self-hosted"})
VALID_DESKTOP_UPDATE_SEVERITIES = {"none", "banner", "required"}


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _as_string(value: Any) -> Optional[str]:
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    return None


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    narrowed: list[object] = cast(list[object], value)
    return [item.strip() for item in narrowed if isinstance(item, str) and item.strip()]


def _is_neutral_deployment() -> bool:
    return os.getenv("OMI_DEPLOYMENT_PROFILE", "").strip().lower() in NEUTRAL_DEPLOYMENT_PROFILES


def _as_download_url(value: Any, *, reject_omi_origin: bool = False) -> Optional[str]:
    candidate = _as_string(value)
    if candidate is None:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    if reject_omi_origin:
        host = (parsed.hostname or "").lower().rstrip(".")
        if host == "omi.me" or host.endswith(".omi.me"):
            return None
    return candidate


def _operator_download_url() -> Optional[str]:
    """Return the explicitly configured operator update URL, if valid.

    Read the environment at the request boundary rather than import time so
    one process cannot retain a URL from a previous deployment profile. The
    URL is optional: neutral deployments intentionally expose a typed
    unavailable policy until the operator publishes a release manifest or
    sets ``DESKTOP_UPDATE_DOWNLOAD_URL``.
    """

    return _as_download_url(
        os.getenv(DESKTOP_UPDATE_DOWNLOAD_URL_ENV),
        reject_omi_origin=_is_neutral_deployment(),
    )


def _default_download_url() -> tuple[Optional[str], Literal["configured", "disabled"], Optional[str]]:
    operator_url = _operator_download_url()
    if operator_url:
        return operator_url, "configured", None
    if _is_neutral_deployment():
        return None, "disabled", "operator_download_url_not_configured"
    return DEFAULT_DESKTOP_DOWNLOAD_URL, "configured", None


def default_desktop_update_policy() -> dict[str, Any]:
    download_url, availability, reason = _default_download_url()
    return {
        "id": "current",
        "active": False,
        "severity": "none",
        "maximum_build_number": None,
        "latest_build_number": None,
        "title": None,
        "message": None,
        "cta_text": "Download latest",
        "download_url": download_url,
        "availability": availability,
        "reason": reason,
        "can_dismiss": True,
    }


def _normalize_policy(data: dict[str, Any]) -> dict[str, Any]:
    policy = default_desktop_update_policy()
    explicit_url = _as_download_url(data.get("download_url"), reject_omi_origin=_is_neutral_deployment())
    download_url = explicit_url or policy["download_url"]
    has_download_url = download_url is not None

    severity = _as_string(data.get("severity")) or "none"
    if severity not in VALID_DESKTOP_UPDATE_SEVERITIES:
        severity = "none"
    maximum_build_number = _as_int(data.get("maximum_build_number"))
    if maximum_build_number is None:
        maximum_build_number = _as_int(data.get("minimum_build_number"))

    policy.update(
        {
            "id": _as_string(data.get("id")) or policy["id"],
            "active": _as_bool(data.get("active")) and has_download_url,
            "severity": severity,
            "maximum_build_number": maximum_build_number,
            "latest_build_number": _as_int(data.get("latest_build_number")),
            "title": _as_string(data.get("title")),
            "message": _as_string(data.get("message")),
            "cta_text": _as_string(data.get("cta_text")) or policy["cta_text"],
            "download_url": download_url,
            "availability": "configured" if has_download_url else "disabled",
            "reason": None if has_download_url else "operator_download_url_not_configured",
            "can_dismiss": _as_bool(data.get("can_dismiss"), default=True),
            "platforms": _as_string_list(data.get("platforms")),
        }
    )
    return policy


def _applies_to_platform(policy: dict[str, Any], platform: str) -> bool:
    raw_platforms = policy.get("platforms")
    platforms: list[object] = cast(list[object], raw_platforms) if isinstance(raw_platforms, list) else []
    if not platforms:
        return True
    return platform in [p for p in platforms if isinstance(p, str)]


def get_desktop_update_policy(
    current_build: Optional[int], platform: str = "macos", *, firestore_client: Any = None
) -> dict[str, Any]:
    client: Any = firestore_client if firestore_client is not None else get_firestore_client()
    doc = client.collection("desktop_update_policy").document("current").get()
    if not getattr(doc, "exists", False):
        return default_desktop_update_policy()

    raw_doc: object = doc.to_dict()
    raw: dict[str, Any] = cast(dict[str, Any], raw_doc) if isinstance(raw_doc, dict) else {}
    policy = _normalize_policy(raw)
    if not _applies_to_platform(policy, platform):
        return default_desktop_update_policy()

    maximum_build = policy.get("maximum_build_number")
    if current_build is not None and maximum_build is not None and current_build > maximum_build:
        return default_desktop_update_policy()

    if not policy["active"]:
        return default_desktop_update_policy()

    return policy
