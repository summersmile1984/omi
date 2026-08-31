#!/usr/bin/env python3
"""Replay one retained desktop manifest from Firestore API into Cloudflare D1.

The legacy endpoint remains the read authority while desktop release promotion
is still on the production backend.  This helper makes the one-way projection
explicit: it reads and validates the immutable manifest, posts the exact bytes
to the Cloudflare manifest endpoint, and verifies the returned identity.  It
never writes a channel pointer and it never sends a raw credential to output.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any


class ManifestBackfillError(RuntimeError):
    """A credential-safe projection or response failure."""


def _load_manifest_contract() -> Any:
    source = Path(__file__).with_name("desktop_release_manifest.py")
    spec = importlib.util.spec_from_file_location("desktop_release_manifest_contract", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("desktop release manifest contract is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CONTRACT = _load_manifest_contract()


def _base_url(value: str, *, label: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{label} must be an https URL without embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not contain a query or fragment")
    return value.rstrip("/")


def _request_json(
    request: urllib.request.Request,
    *,
    opener: Callable[..., Any],
    error_context: str,
) -> dict[str, Any]:
    try:
        with opener(request, timeout=30) as response:
            raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise ManifestBackfillError(f"{error_context} returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise ManifestBackfillError(f"{error_context} was unavailable: {type(error).__name__}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestBackfillError(f"{error_context} returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise ManifestBackfillError(f"{error_context} returned an unexpected payload")
    return payload


def _manifest_digest(manifest: dict[str, Any]) -> str:
    # The public v2 response historically exposes the detached digest without
    # the ``sha256:`` artifact prefix used by the manifest's internal fields.
    return _CONTRACT.manifest_digest(manifest).removeprefix("sha256:")


def fetch_legacy_manifest(
    release_id: str,
    *,
    legacy_base_url: str,
    admin_key: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Read one immutable manifest from the legacy API and verify its digest."""
    if not admin_key:
        raise ManifestBackfillError("admin key is not configured")
    base = _base_url(legacy_base_url, label="legacy_base_url")
    encoded_id = urllib.parse.quote(release_id, safe="")
    request = urllib.request.Request(
        f"{base}/v2/desktop/releases/{encoded_id}",
        headers={"Accept": "application/json", "secret-key": admin_key},
        method="GET",
    )
    payload = _request_json(request, opener=opener, error_context="legacy desktop manifest read")
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        raise ManifestBackfillError("legacy desktop manifest response is missing manifest")
    try:
        validated = _CONTRACT.validate_manifest(manifest)
    except ValueError as error:
        raise ManifestBackfillError("legacy desktop manifest violates the v1 contract") from error
    if validated.get("release_id") != release_id:
        raise ManifestBackfillError("legacy desktop manifest release_id does not match the requested release")
    supplied_digest = payload.get("manifest_sha256")
    if supplied_digest is not None and supplied_digest != _manifest_digest(validated):
        raise ManifestBackfillError("legacy desktop manifest digest does not match its canonical bytes")
    return validated


def publish_cloudflare_manifest(
    manifest: dict[str, Any],
    *,
    target_base_url: str,
    admin_key: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Register one manifest in Cloudflare D1 and verify the idempotent reply."""
    if not admin_key:
        raise ManifestBackfillError("admin key is not configured")
    base = _base_url(target_base_url, label="target_base_url")
    try:
        validated = _CONTRACT.validate_manifest(manifest)
    except ValueError as error:
        raise ManifestBackfillError("desktop manifest violates the v1 contract") from error
    body = json.dumps(validated, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/v2/desktop/releases",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "secret-key": admin_key,
        },
        method="POST",
    )
    payload = _request_json(request, opener=opener, error_context="Cloudflare desktop manifest registration")
    returned = payload.get("manifest")
    if returned != validated:
        raise ManifestBackfillError("Cloudflare desktop manifest response differs from the immutable source")
    supplied_digest = payload.get("manifest_sha256")
    if supplied_digest is not None and supplied_digest != _manifest_digest(validated):
        raise ManifestBackfillError("Cloudflare desktop manifest digest does not match its canonical bytes")
    return validated


def backfill_manifest(
    release_id: str,
    *,
    legacy_base_url: str,
    target_base_url: str,
    admin_key: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, str]:
    manifest = fetch_legacy_manifest(
        release_id,
        legacy_base_url=legacy_base_url,
        admin_key=admin_key,
        opener=opener,
    )
    published = publish_cloudflare_manifest(
        manifest,
        target_base_url=target_base_url,
        admin_key=admin_key,
        opener=opener,
    )
    return {
        "release_id": str(published["release_id"]),
        "manifest_sha256": _manifest_digest(published),
        "legacy_endpoint": f"{_base_url(legacy_base_url, label='legacy_base_url')}/v2/desktop/releases/{urllib.parse.quote(release_id, safe='')}",
        "cloudflare_endpoint": f"{_base_url(target_base_url, label='target_base_url')}/v2/desktop/releases",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True, help="exact v<version>+<build>-macos release id")
    parser.add_argument(
        "--legacy-base-url",
        default=os.getenv("DESKTOP_LEGACY_API_BASE_URL", "https://api.omi.me"),
        help="legacy API base URL (default: DESKTOP_LEGACY_API_BASE_URL or https://api.omi.me)",
    )
    parser.add_argument(
        "--target-base-url",
        required=True,
        help="Cloudflare Edge base URL; required to prevent accidental production writes",
    )
    parser.add_argument(
        "--admin-key-env",
        default="ADMIN_KEY",
        help="environment variable containing the admin key (default: ADMIN_KEY)",
    )
    args = parser.parse_args()
    admin_key = os.getenv(args.admin_key_env, "")
    try:
        result = backfill_manifest(
            args.release_id,
            legacy_base_url=args.legacy_base_url,
            target_base_url=args.target_base_url,
            admin_key=admin_key,
        )
    except (ManifestBackfillError, ValueError, OSError) as error:
        raise SystemExit(f"FAIL: {error}") from error
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
