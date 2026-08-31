"""Public desktop update feeds backed by the Cloudflare D1 projection.

The release pipeline owns artifact publication and promotion. This Worker only
serves signed metadata that has been explicitly projected into D1; an empty
projection therefore fails closed for downloads and latest-version lookups.
"""

from __future__ import annotations

import html
import hashlib
import hmac
import json
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from fallback import record_fallback

router = APIRouter()

DEFAULT_DOWNLOAD_URL = "https://api.omi.me/v2/desktop/download/latest?channel=stable"
VALID_PLATFORMS = {"macos", "windows", "linux"}
VALID_SEVERITIES = {"none", "banner", "required"}
PREVIEW_BUCKET_HOST = "storage.googleapis.com"
PREVIEW_BUCKET_NAME = "omi_macos_updates"
PREVIEW_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
PREVIEW_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
PREVIEW_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DESKTOP_RELEASE_ID_RE = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+(?:\.[0-9]+)?)\+(?P<build>[1-9][0-9]*)-macos$")
DESKTOP_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DESKTOP_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DESKTOP_EVIDENCE_RE = re.compile(r"^qualification-evidence-[^/]+\.json$")
DESKTOP_ENVIRONMENT_RE = re.compile(r"^desktop-backend-env-v[1-9][0-9]*$")
DESKTOP_BETA_TAG_RE = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+(?:\.[0-9]+)?)\+(?P<build>[1-9][0-9]*)-macos$")
DESKTOP_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "release_id",
        "platform",
        "version",
        "build_number",
        "app_source_sha",
        "zip_url",
        "zip_sha256",
        "dmg_url",
        "dmg_sha256",
        "ed_signature",
        "qualification_evidence_asset",
        "qualification_evidence_sha256",
        "qualification_tier",
        "qualification_passed",
        "backend_mode",
        "desktop_backend_source_sha",
        "desktop_backend_oci_index_digest",
        "desktop_backend_platform_digest",
        "compatibility_contract",
        "environment_contract_version",
        "created_at",
        "published_at",
        "changelog",
        "mandatory",
    }
)
DESKTOP_MANIFEST_REQUIRED_FIELDS = DESKTOP_MANIFEST_FIELDS - {
    "desktop_backend_source_sha",
    "desktop_backend_oci_index_digest",
    "desktop_backend_platform_digest",
    "published_at",
    "changelog",
    "mandatory",
}
DESKTOP_BACKEND_FIELDS = frozenset(
    {
        "desktop_backend_source_sha",
        "desktop_backend_oci_index_digest",
        "desktop_backend_platform_digest",
    }
)


class DesktopPreviewDelistRequest(BaseModel):
    """Compare-and-delete request for a mutable preview landing-page pointer."""

    expected_generation: int = Field(ge=0)


class DesktopPreviewPublishRequest(BaseModel):
    """Immutable metadata for a signed desktop preview artifact."""

    slug: str
    source_sha: str
    dmg_url: str
    dmg_sha256: str
    app_name: str
    bundle_id: str
    url_scheme: str
    built_at: str
    signer: str
    notarization: str
    notes: str | None = None
    backend_url: str | None = None
    expected_generation: int | None = Field(default=None, ge=0)


def _https_url(value: object, default: str | None = None) -> str | None:
    candidate = value.strip() if isinstance(value, str) else ""
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return default
    return candidate


def _preview_slug(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if PREVIEW_SLUG_RE.fullmatch(candidate) else None


def _preview_source_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if PREVIEW_SHA40_RE.fullmatch(candidate) else None


def _preview_identity(slug: str) -> str:
    return f"p{hashlib.sha256(slug.encode('utf-8')).hexdigest()[:10]}"


def _normalize_preview_publish(payload: DesktopPreviewPublishRequest) -> dict[str, object]:
    slug = payload.slug.strip()
    if _preview_slug(slug) is None:
        raise ValueError("slug must use lowercase letters, digits, and path-safe hyphens")
    source_sha = payload.source_sha.strip().lower()
    if _preview_source_sha(source_sha) is None:
        raise ValueError("source_sha must be a full 40-character commit SHA")
    app_name = payload.app_name.strip()
    if not app_name or len(app_name) > 128 or not app_name.startswith("Omi Preview"):
        raise ValueError("app_name must identify this as an Omi Preview build")
    preview_id = _preview_identity(slug)
    if payload.bundle_id.strip() != f"com.omi.preview.{preview_id}":
        raise ValueError("bundle_id must match the slug-derived com.omi.preview.<id> identity")
    if payload.url_scheme.strip() != f"omi-preview-{preview_id}":
        raise ValueError("url_scheme must match the slug-derived omi-preview-<id> identity")
    if payload.notarization.strip().lower() != "stapled":
        raise ValueError("notarization must be stapled")
    dmg_sha256 = payload.dmg_sha256.strip().lower()
    if not PREVIEW_SHA256_RE.fullmatch(dmg_sha256):
        raise ValueError("dmg_sha256 must be a SHA-256 digest")
    built_at = payload.built_at.strip()
    if len(built_at) > 64:
        raise ValueError("built_at is too long")
    try:
        datetime.fromisoformat(built_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("built_at must be an ISO-8601 timestamp") from exc
    signer = payload.signer.strip()
    if not signer or len(signer) > 512:
        raise ValueError("signer is required")

    dmg_url = payload.dmg_url.strip()
    if len(dmg_url) > 2_048:
        raise ValueError("dmg_url is too long")
    parsed_dmg = urlparse(dmg_url)
    expected_path = f"/{PREVIEW_BUCKET_NAME}/previews/{slug}/{source_sha}/Omi-Preview.dmg"
    if (
        parsed_dmg.scheme != "https"
        or parsed_dmg.netloc != PREVIEW_BUCKET_HOST
        or parsed_dmg.path != expected_path
        or parsed_dmg.params
        or parsed_dmg.query
        or parsed_dmg.fragment
    ):
        raise ValueError("dmg_url must be the canonical immutable preview artifact URL")

    notes = payload.notes.strip() if isinstance(payload.notes, str) else None
    if notes == "":
        notes = None
    if notes is not None and len(notes) > 2_000:
        raise ValueError("notes is too long")
    backend_url = payload.backend_url.strip() if isinstance(payload.backend_url, str) else None
    if backend_url == "":
        backend_url = None
    if backend_url is not None:
        if len(backend_url) > 2_048:
            raise ValueError("backend_url is too long")
        parsed_backend = urlparse(backend_url)
        if (
            parsed_backend.scheme != "https"
            or not parsed_backend.netloc
            or parsed_backend.params
            or parsed_backend.query
            or parsed_backend.fragment
        ):
            raise ValueError("backend_url must be an https URL")
    return {
        "slug": slug,
        "source_sha": source_sha,
        "dmg_url": dmg_url,
        "dmg_sha256": dmg_sha256,
        "app_name": app_name,
        "bundle_id": f"com.omi.preview.{preview_id}",
        "url_scheme": f"omi-preview-{preview_id}",
        "built_at": built_at,
        "signer": signer,
        "notarization": "stapled",
        "notes": notes,
        "backend_url": backend_url,
    }


def _preview_manifest(row: object, *, slug: str, source_sha: str) -> dict[str, object] | None:
    if not isinstance(row, dict):
        return None
    if _preview_slug(row.get("slug")) != slug or _preview_source_sha(row.get("source_sha")) != source_sha:
        return None
    app_name = row.get("app_name")
    bundle_id = row.get("bundle_id")
    url_scheme = row.get("url_scheme")
    built_at = row.get("built_at")
    signer = row.get("signer")
    notarization = row.get("notarization")
    dmg_sha256 = row.get("dmg_sha256")
    if (
        not isinstance(app_name, str)
        or not app_name.startswith("Omi Preview")
        or not isinstance(bundle_id, str)
        or bundle_id != f"com.omi.preview.{_preview_identity(slug)}"
        or not isinstance(url_scheme, str)
        or url_scheme != f"omi-preview-{_preview_identity(slug)}"
        or not isinstance(built_at, str)
        or not built_at.strip()
        or not isinstance(signer, str)
        or not signer.strip()
        or notarization != "stapled"
        or not isinstance(dmg_sha256, str)
        or not PREVIEW_SHA256_RE.fullmatch(dmg_sha256.lower())
    ):
        return None
    try:
        datetime.fromisoformat(built_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    dmg_url = row.get("dmg_url")
    parsed = urlparse(dmg_url) if isinstance(dmg_url, str) else None
    expected_path = f"/{PREVIEW_BUCKET_NAME}/previews/{slug}/{source_sha}/Omi-Preview.dmg"
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.netloc != PREVIEW_BUCKET_HOST
        or parsed.path != expected_path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    notes = row.get("notes")
    if notes is not None and not isinstance(notes, str):
        return None
    backend_url = row.get("backend_url")
    if backend_url is not None:
        backend_parsed = urlparse(backend_url) if isinstance(backend_url, str) else None
        if (
            backend_parsed is None
            or backend_parsed.scheme != "https"
            or not backend_parsed.netloc
            or backend_parsed.params
            or backend_parsed.query
            or backend_parsed.fragment
        ):
            return None
    return {
        "slug": slug,
        "source_sha": source_sha,
        "dmg_url": dmg_url,
        "dmg_sha256": dmg_sha256.lower(),
        "app_name": app_name,
        "bundle_id": bundle_id,
        "url_scheme": url_scheme,
        "built_at": built_at,
        "signer": signer,
        "notarization": notarization,
        "notes": notes,
        "backend_url": backend_url,
    }


def _manual_download_url(release: dict[str, object]) -> str:
    explicit = release.get("manual_download_url")
    if isinstance(explicit, str) and explicit:
        return explicit
    download_url = str(release["download_url"])
    if download_url.endswith("/Omi.zip"):
        return f"{download_url[:-len('Omi.zip')]}Omi.dmg"
    return download_url


def _download_landing_html(url: str, *, platform: str, channel: str, version: str, notice: str = "") -> str:
    escaped_url = html.escape(url, quote=True)
    escaped_version = html.escape(version)
    escaped_channel = "Beta " if channel == "beta" else ""
    escaped_notice = f'<p class="notice">{html.escape(notice)}</p>' if notice else ""
    os_name = "Windows" if platform == "windows" else "macOS"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="2;url={escaped_url}">
  <title>Download Omi {escaped_channel}for {os_name}</title>
  <style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0a0a0a;color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;text-align:center}}main{{max-width:620px;padding:40px}}a{{color:#fff}}.notice{{color:#fbbf24}}.version{{color:#999}}</style>
</head>
<body><main>
  <h1>Downloading Omi {escaped_channel}for {os_name}</h1>
  <p class="version">v{escaped_version}</p>
  {escaped_notice}
  <p>Your download should start automatically.</p>
  <p><a href="{escaped_url}">Click here if the download does not start</a></p>
</main></body>
</html>"""


def _bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return default


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _changelog(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    return [item.strip() for item in decoded if isinstance(item, str) and item.strip()][:100]


def _release(row: dict[str, object]) -> dict[str, object] | None:
    version = row.get("version")
    download_url = _https_url(row.get("download_url"))
    signature = row.get("ed_signature")
    published_at = row.get("published_at")
    channel = row.get("channel")
    build_number = _int(row.get("build_number"))
    if not all(isinstance(value, str) and value for value in (version, signature, published_at)):
        return None
    if download_url is None or build_number is None or channel not in {"staging", "beta", "stable"}:
        return None
    return {
        "version": version,
        "build_number": build_number,
        "download_url": download_url,
        "manual_download_url": _https_url(row.get("manual_download_url")),
        "ed_signature": signature,
        "published_at": published_at,
        "changelog": _changelog(row.get("changelog_json")),
        "is_live": _bool(row.get("is_live")),
        "is_critical": _bool(row.get("is_critical")),
        "channel": channel,
        "windows_feed_url": _https_url(row.get("windows_feed_url")),
    }


def _manifest_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or not value.endswith("Z"):
        raise ValueError(f"{field} must be an RFC 3339 UTC timestamp")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC 3339 UTC timestamp") from exc
    return value


def _manifest_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _manifest_digest(value: object, field: str) -> str:
    digest = _manifest_string(value, field)
    if not DESKTOP_DIGEST_RE.fullmatch(digest):
        raise ValueError(f"{field} must use sha256:<64 lowercase hex> form")
    return digest


def _manifest_source_sha(value: object, field: str) -> str:
    digest = _manifest_string(value, field)
    if not DESKTOP_SOURCE_SHA_RE.fullmatch(digest):
        raise ValueError(f"{field} must be a lowercase 40-character Git SHA")
    return digest


def _manifest_asset_url(value: object, field: str, *, release_id: str, asset_name: str) -> str:
    url = _manifest_string(value, field)
    parsed = urlparse(url)
    expected = f"/BasedHardware/omi/releases/download/{release_id}/{asset_name}"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != expected
    ):
        raise ValueError(f"{field} must reference {asset_name} on release {release_id}")
    return url


def _validate_desktop_manifest(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("manifest must be a JSON object")
    manifest = value
    missing = sorted(DESKTOP_MANIFEST_REQUIRED_FIELDS - manifest.keys())
    unknown = sorted(manifest.keys() - DESKTOP_MANIFEST_FIELDS)
    if missing:
        raise ValueError(f"manifest is missing required field(s): {', '.join(missing)}")
    if unknown:
        raise ValueError(f"manifest has unknown field(s): {', '.join(unknown)}")
    if manifest.get("schema_version") != 1 or manifest.get("platform") != "macos":
        raise ValueError("manifest must use schema_version 1 and platform macos")

    release_id = _manifest_string(manifest.get("release_id"), "release_id")
    match = DESKTOP_RELEASE_ID_RE.fullmatch(release_id)
    if match is None:
        raise ValueError("release_id must use v<version>+<build>-macos form")
    version = _manifest_string(manifest.get("version"), "version")
    build_number = manifest.get("build_number")
    if not isinstance(build_number, int) or isinstance(build_number, bool) or build_number <= 0:
        raise ValueError("build_number must be a positive integer")
    if version != match.group("version") or build_number != int(match.group("build")):
        raise ValueError("version and build_number must match release_id")

    _manifest_source_sha(manifest.get("app_source_sha"), "app_source_sha")
    _manifest_asset_url(manifest.get("zip_url"), "zip_url", release_id=release_id, asset_name="Omi.zip")
    _manifest_digest(manifest.get("zip_sha256"), "zip_sha256")
    _manifest_asset_url(manifest.get("dmg_url"), "dmg_url", release_id=release_id, asset_name="omi.dmg")
    _manifest_digest(manifest.get("dmg_sha256"), "dmg_sha256")
    _manifest_string(manifest.get("ed_signature"), "ed_signature")

    evidence_asset = _manifest_string(manifest.get("qualification_evidence_asset"), "qualification_evidence_asset")
    if not DESKTOP_EVIDENCE_RE.fullmatch(evidence_asset) and evidence_asset not in {
        "desktop-smoke-result.json",
        "desktop-smoke-result-beta.json",
    }:
        raise ValueError("qualification_evidence_asset is not a recognized evidence asset")
    _manifest_digest(manifest.get("qualification_evidence_sha256"), "qualification_evidence_sha256")
    qualification = (manifest.get("qualification_tier"), manifest.get("qualification_passed"))
    if qualification not in {("T2", True), ("signed-smoke", False), ("emergency", False)}:
        raise ValueError("release evidence must be T2, signed-smoke, or emergency truth")
    if qualification == ("T2", True) and not DESKTOP_EVIDENCE_RE.fullmatch(evidence_asset):
        raise ValueError("T2 qualification requires a qualification-evidence-*.json asset")
    if qualification == ("emergency", False) and evidence_asset != "desktop-smoke-result.json":
        raise ValueError("emergency qualification requires exact signed-smoke evidence")
    if qualification == ("signed-smoke", False) and evidence_asset != "desktop-smoke-result-beta.json":
        raise ValueError("normal Beta promotion requires exact Codemagic Beta signed-smoke evidence")

    mode = manifest.get("backend_mode")
    if mode not in {"app_only", "backend_required"}:
        raise ValueError("backend_mode must be app_only or backend_required")
    present_backend_fields = DESKTOP_BACKEND_FIELDS & manifest.keys()
    if mode == "app_only" and present_backend_fields:
        raise ValueError("app_only manifest must omit backend fields")
    if mode == "backend_required":
        if present_backend_fields != DESKTOP_BACKEND_FIELDS:
            raise ValueError("backend_required manifest must include all backend fields")
        _manifest_source_sha(manifest.get("desktop_backend_source_sha"), "desktop_backend_source_sha")
        if manifest["desktop_backend_source_sha"] != manifest["app_source_sha"]:
            raise ValueError("desktop backend and app must come from the same source SHA")
        index_digest = _manifest_digest(
            manifest.get("desktop_backend_oci_index_digest"), "desktop_backend_oci_index_digest"
        )
        platform_digest = _manifest_digest(
            manifest.get("desktop_backend_platform_digest"), "desktop_backend_platform_digest"
        )
        if index_digest == platform_digest:
            raise ValueError("OCI index and platform-child digests must identify distinct objects")

    environment_contract = _manifest_string(
        manifest.get("environment_contract_version"), "environment_contract_version"
    )
    if not DESKTOP_ENVIRONMENT_RE.fullmatch(environment_contract):
        raise ValueError("environment_contract_version must use desktop-backend-env-vN form")
    _manifest_timestamp(manifest.get("created_at"), "created_at")
    if "published_at" in manifest:
        _manifest_timestamp(manifest["published_at"], "published_at")
    if "changelog" in manifest:
        changelog = manifest["changelog"]
        if not isinstance(changelog, list) or any(not isinstance(item, str) or not item.strip() for item in changelog):
            raise ValueError("changelog must be a list of non-empty strings")
    if "mandatory" in manifest and not isinstance(manifest["mandatory"], bool):
        raise ValueError("mandatory must be a boolean")

    compatibility = manifest.get("compatibility_contract")
    if not isinstance(compatibility, dict):
        raise ValueError("compatibility_contract must be an object")
    expected_compatibility = {
        "schema_version": 1,
        "app_release_id": release_id,
        "app_version": version,
        "app_build_number": build_number,
        "backend_mode": mode,
        "environment_contract_version": environment_contract,
    }
    if mode == "backend_required":
        expected_compatibility.update({field: manifest[field] for field in DESKTOP_BACKEND_FIELDS})
    if compatibility != expected_compatibility:
        raise ValueError("compatibility_contract must exactly match the manifest")
    return manifest


def _manifest_canonical_bytes(manifest: dict[str, object]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _manifest_sha256(manifest: dict[str, object]) -> str:
    return hashlib.sha256(_manifest_canonical_bytes(manifest)).hexdigest()


def _release_id_parts(release_id: object) -> tuple[str, str, int]:
    if not isinstance(release_id, str):
        raise ValueError("release_id must use v<version>+<build>-macos form")
    match = DESKTOP_BETA_TAG_RE.fullmatch(release_id)
    if match is None:
        raise ValueError("release_id must use v<version>+<build>-macos form")
    version_text = match.group("version")
    return release_id, version_text, int(match.group("build"))


def _pointer_from_row(row: object, *, platform: str, channel: str) -> dict[str, object] | None:
    if not isinstance(row, dict):
        return None
    if row.get("platform") != platform or row.get("channel") != channel:
        return None
    release_id, version_text, build = _release_id_parts(row.get("release_id"))
    if row.get("version") != version_text or row.get("build_number") != build:
        return None
    generation = row.get("generation")
    updated_at = row.get("updated_at")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        return None
    if not isinstance(updated_at, int) or isinstance(updated_at, bool) or updated_at <= 0:
        return None
    return {
        "platform": platform,
        "channel": channel,
        "release_id": release_id,
        "version": version_text,
        "build_number": build,
        "generation": generation,
        "updated_at": updated_at,
    }


def _accepted_pointer_manifest(manifest: dict[str, object]) -> None:
    if (manifest.get("qualification_tier"), manifest.get("qualification_passed")) not in {
        ("T2", True),
        ("signed-smoke", False),
    }:
        raise ValueError("release manifest qualification is missing accepted normal-path evidence")


def _changes(result: object) -> int:
    if not isinstance(result, dict):
        return 0
    meta = result.get("meta")
    if not isinstance(meta, dict):
        return 0
    value = meta.get("changes")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _manifest_from_row(row: object, release_id: str) -> tuple[dict[str, object], str] | None:
    if not isinstance(row, dict):
        return None
    raw_json = row.get("manifest_json")
    stored_digest = row.get("manifest_sha256")
    if not isinstance(raw_json, str) or not isinstance(stored_digest, str):
        return None
    try:
        manifest = json.loads(raw_json)
        validated = _validate_desktop_manifest(manifest)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if validated.get("release_id") != release_id or not hmac.compare_digest(stored_digest, _manifest_sha256(validated)):
        return None
    return validated, stored_digest


def _release_manifest_key_valid(request: Request) -> bool:
    expected = getattr(request.scope["env"], "ADMIN_KEY", None)
    provided = request.headers.get("secret-key")
    return (
        isinstance(expected, str)
        and bool(expected)
        and isinstance(provided, str)
        and hmac.compare_digest(provided, expected)
    )


async def _register_release_manifest(request: Request, payload: dict[str, object]) -> dict[str, object]:
    manifest = _validate_desktop_manifest(payload)
    release_id = str(manifest["release_id"])
    digest = _manifest_sha256(manifest)
    env = request.scope["env"]
    existing = await (
        env.APP_DB.prepare(
            "SELECT release_id, manifest_json, manifest_sha256 FROM cf_desktop_release_manifests "
            "WHERE release_id = ? LIMIT 1"
        )
        .bind(release_id)
        .first()
    )
    if existing is not None:
        parsed = _manifest_from_row(existing, release_id)
        if parsed is None or parsed[0] != manifest or parsed[1] != digest:
            raise ValueError("release_id already exists with different immutable metadata")
        return manifest
    try:
        await (
            env.APP_DB.prepare(
                "INSERT INTO cf_desktop_release_manifests "
                "(release_id, manifest_json, manifest_sha256, created_at) VALUES (?, ?, ?, unixepoch())"
            )
            .bind(release_id, _manifest_canonical_bytes(manifest).decode("utf-8"), digest)
            .run()
        )
    except Exception as exc:
        raise ValueError("release_id already exists or manifest projection is unavailable") from exc
    return manifest


async def _promote_stable_pointer(request: Request, payload: dict[str, object]) -> dict[str, object]:
    """Advance the D1 macOS Stable pointer with explicit CAS preconditions.

    The immutable manifest must already be projected.  A pointer write never
    copies caller-supplied metadata and therefore cannot publish an artifact
    that was not validated by the manifest contract first.
    """
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    allowed = {
        "platform",
        "channel",
        "release_id",
        "expected_generation",
        "expected_current_release_id",
        "operation",
    }
    if set(payload) - allowed:
        raise ValueError("request contains unknown fields")
    platform = payload.get("platform")
    channel = payload.get("channel")
    release_id = payload.get("release_id")
    operation = payload.get("operation", "promote")
    if platform != "macos" or channel != "stable":
        raise ValueError("generic channel promotion is macos stable-only")
    if not isinstance(release_id, str):
        raise ValueError("release_id is required")
    release_id = release_id.strip()
    _, version_text, build = _release_id_parts(release_id)
    if operation not in {"promote", "repoint"}:
        raise ValueError("invalid pointer operation")
    expected_generation = payload.get("expected_generation")
    if expected_generation is not None and (
        not isinstance(expected_generation, int) or isinstance(expected_generation, bool) or expected_generation < 0
    ):
        raise ValueError("expected_generation must be a non-negative integer")
    expected_current = payload.get("expected_current_release_id")
    if expected_current is not None and not isinstance(expected_current, str):
        raise ValueError("expected_current_release_id must be a string")
    if operation == "repoint" and (expected_generation is None or expected_current is None):
        raise ValueError("repoint requires expected_current_release_id and expected_generation")

    db = request.scope["env"].APP_DB
    manifest_row = await (
        db.prepare(
            "SELECT release_id, manifest_json, manifest_sha256 FROM cf_desktop_release_manifests "
            "WHERE release_id = ? LIMIT 1"
        )
        .bind(release_id)
        .first()
    )
    parsed_manifest = _manifest_from_row(manifest_row, release_id)
    if parsed_manifest is None:
        raise ValueError("release manifest does not exist or is invalid")
    manifest, _ = parsed_manifest
    _accepted_pointer_manifest(manifest)
    if manifest["version"] != version_text or manifest["build_number"] != build:
        raise ValueError("release_id does not match the retained manifest")

    current_row = await db.prepare(
        "SELECT platform, channel, release_id, version, build_number, generation, updated_at "
        "FROM cf_desktop_channel_pointers WHERE platform = 'macos' AND channel = 'stable' LIMIT 1"
    ).first()
    current = _pointer_from_row(current_row, platform="macos", channel="stable") if current_row else None
    if current_row is not None and current is None:
        raise ValueError("stable channel pointer projection is invalid")
    if current is not None and current["release_id"] == release_id:
        return {"success": True, "pointer": current, "idempotent": True}
    current_generation = int(current["generation"]) if current is not None else 0
    if expected_generation is not None and expected_generation != current_generation:
        raise ValueError(f"generation mismatch: expected {expected_generation}, current {current_generation}")
    if operation == "repoint" and current is not None and current["release_id"] != expected_current:
        raise ValueError(f"current release mismatch: expected {expected_current}, current {current['release_id']}")
    if operation == "repoint" and current is None and expected_current:
        raise ValueError("current release mismatch: expected existing stable pointer")
    if operation == "promote" and current is not None and build <= int(current["build_number"]):
        raise ValueError("channel pointers are roll-forward only")

    now = int(time.time())
    pointer = {
        "platform": "macos",
        "channel": "stable",
        "release_id": release_id,
        "version": version_text,
        "build_number": build,
        "generation": current_generation + 1,
        "updated_at": now,
    }
    if current is None:
        result = (
            await db.prepare(
                "INSERT INTO cf_desktop_channel_pointers "
                "(platform, channel, release_id, version, build_number, generation, updated_at) "
                "SELECT 'macos', 'stable', ?, ?, ?, 1, ? WHERE NOT EXISTS "
                "(SELECT 1 FROM cf_desktop_channel_pointers WHERE platform = 'macos' AND channel = 'stable')"
            )
            .bind(release_id, version_text, build, now)
            .run()
        )
    else:
        result = (
            await db.prepare(
                "UPDATE cf_desktop_channel_pointers SET release_id = ?, version = ?, build_number = ?, "
                "generation = ?, updated_at = ? WHERE platform = 'macos' AND channel = 'stable' AND generation = ?"
            )
            .bind(release_id, version_text, build, current_generation + 1, now, current_generation)
            .run()
        )
    if _changes(result) != 1:
        raise ValueError("stable channel pointer changed during promotion")
    return {"success": True, "pointer": pointer, "idempotent": False}


async def _live_releases(request: Request) -> list[dict[str, object]]:
    rows = (
        await request.scope["env"]
        .APP_DB.prepare(
            "SELECT version, build_number, download_url, manual_download_url, ed_signature, published_at, "
            "changelog_json, is_live, is_critical, channel, windows_feed_url "
            "FROM cf_desktop_releases WHERE is_live = 1 ORDER BY build_number DESC, id DESC LIMIT 100"
        )
        .all()
    )
    raw_rows = rows.get("results", []) if isinstance(rows, dict) else []
    legacy_releases = [release for row in raw_rows if isinstance(row, dict) and (release := _release(row)) is not None]

    # Once a channel pointer exists, it is the authoritative macOS release for
    # that channel.  Keep the older release projection as a compatibility
    # fallback until the first pointer is backfilled.
    pointer_rows = (
        await request.scope["env"]
        .APP_DB.prepare(
            "SELECT platform, channel, release_id, version, build_number, generation, updated_at "
            "FROM cf_desktop_channel_pointers WHERE platform = 'macos' AND channel IN ('beta', 'stable')"
        )
        .all()
    )
    projected: list[dict[str, object]] = []
    for pointer_row in (pointer_rows.get("results", []) if isinstance(pointer_rows, dict) else []):
        if not isinstance(pointer_row, dict):
            continue
        channel = pointer_row.get("channel")
        pointer = (
            _pointer_from_row(pointer_row, platform="macos", channel=str(channel))
            if channel in {"beta", "stable"}
            else None
        )
        if pointer is None:
            raise ValueError("desktop channel pointer projection is invalid")
        manifest_row = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT release_id, manifest_json, manifest_sha256 FROM cf_desktop_release_manifests WHERE release_id = ? LIMIT 1"
            )
            .bind(pointer["release_id"])
            .first()
        )
        parsed = _manifest_from_row(manifest_row, str(pointer["release_id"]))
        if parsed is None:
            raise ValueError("desktop channel pointer references an invalid manifest")
        manifest, _ = parsed
        if (
            manifest["platform"] != "macos"
            or manifest["version"] != pointer["version"]
            or manifest["build_number"] != pointer["build_number"]
        ):
            raise ValueError("desktop channel pointer does not match its manifest")
        projected.append(
            {
                "version": manifest["version"],
                "build_number": manifest["build_number"],
                "download_url": manifest["zip_url"],
                "manual_download_url": manifest["dmg_url"],
                "ed_signature": manifest["ed_signature"],
                "published_at": manifest.get("published_at") or manifest["created_at"],
                "changelog": list(manifest.get("changelog") or []),
                "is_live": True,
                "is_critical": bool(manifest.get("mandatory", False)),
                "channel": channel,
                "windows_feed_url": None,
            }
        )
    if not projected:
        return legacy_releases
    projected_channels = {str(release["channel"]) for release in projected}
    return [release for release in legacy_releases if str(release["channel"]) not in projected_channels] + projected


def _default_policy() -> dict[str, object]:
    return {
        "id": "current",
        "active": False,
        "severity": "none",
        "maximum_build_number": None,
        "latest_build_number": None,
        "title": None,
        "message": None,
        "cta_text": "Download latest",
        "download_url": DEFAULT_DOWNLOAD_URL,
        "can_dismiss": True,
    }


def _policy(row: dict[str, object] | None, *, platform: str, current_build: int | None) -> dict[str, object]:
    if not row:
        return _default_policy()
    severity = row.get("severity") if row.get("severity") in VALID_SEVERITIES else "none"
    platforms: list[str] = []
    raw_platforms = row.get("platforms_json")
    if isinstance(raw_platforms, str):
        try:
            parsed = json.loads(raw_platforms)
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, list):
            platforms = [item.strip() for item in parsed if isinstance(item, str) and item.strip()][:10]
    maximum = _int(row.get("maximum_build_number"))
    if platforms and platform not in platforms:
        return _default_policy()
    if current_build is not None and maximum is not None and current_build > maximum:
        return _default_policy()
    if not _bool(row.get("active")):
        return _default_policy()
    return {
        "id": row.get("id") if isinstance(row.get("id"), str) and row.get("id") else "current",
        "active": True,
        "severity": severity,
        "maximum_build_number": maximum,
        "latest_build_number": _int(row.get("latest_build_number")),
        "title": row.get("title") if isinstance(row.get("title"), str) and row.get("title").strip() else None,
        "message": row.get("message") if isinstance(row.get("message"), str) and row.get("message").strip() else None,
        "cta_text": (
            row.get("cta_text")
            if isinstance(row.get("cta_text"), str) and row.get("cta_text").strip()
            else "Download latest"
        ),
        "download_url": _https_url(row.get("download_url"), DEFAULT_DOWNLOAD_URL),
        "can_dismiss": _bool(row.get("can_dismiss"), default=True),
        "platforms": platforms,
    }


def _appcast_xml(releases: list[dict[str, object]], platform: str) -> str:
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">',
        "  <channel>",
        "    <title>Omi Desktop Updates</title>",
        "    <description>Omi AI Desktop Application</description>",
        "    <language>en</language>",
    ]
    seen_channels: set[str] = set()
    for release in releases:
        channel = str(release["channel"])
        if channel in seen_channels:
            continue
        seen_channels.add(channel)
        changes = "<p>Bug fixes and improvements.</p>"
        changelog = release["changelog"]
        if isinstance(changelog, list) and changelog:
            changes = "<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in changelog) + "</ul>"
        safe_cdata = changes.replace("]]>", "]] ]]><![CDATA[>".replace(" ", ""))
        lines.extend(
            [
                "    <item>",
                f"      <title>Omi {html.escape(str(release['version']))}</title>",
                f"      <sparkle:version>{release['build_number']}</sparkle:version>",
                f"      <sparkle:shortVersionString>{html.escape(str(release['version']))}</sparkle:shortVersionString>",
                f"      <description><![CDATA[{safe_cdata}]]></description>",
                f"      <pubDate>{html.escape(str(release['published_at']))}</pubDate>",
                f'      <enclosure url="{html.escape(str(release["download_url"]), quote=True)}" type="application/octet-stream" sparkle:os="{html.escape(platform, quote=True)}" sparkle:edSignature="{html.escape(str(release["ed_signature"]), quote=True)}" />',
            ]
        )
        if channel != "stable":
            lines.append(f"      <sparkle:channel>{html.escape(channel)}</sparkle:channel>")
        if release["is_critical"]:
            lines.append("      <sparkle:criticalUpdate />")
        lines.append("    </item>")
    lines.extend(["  </channel>", "</rss>", ""])
    return "\n".join(lines)


@router.get("/appcast.xml")
async def get_appcast(request: Request, platform: str = Query(default="macos")):
    if platform not in VALID_PLATFORMS:
        raise HTTPException(status_code=422, detail="Invalid platform")
    try:
        releases = await _live_releases(request)
    except Exception:
        return Response("Desktop update feed is temporarily unavailable", status_code=503, media_type="text/plain")
    return Response(
        _appcast_xml(releases, platform), media_type="application/xml", headers={"Cache-Control": "max-age=300"}
    )


async def _stable_release(request: Request) -> dict[str, object]:
    releases = await _live_releases(request)
    for release in releases:
        if release["channel"] == "stable":
            return release
    raise HTTPException(status_code=404, detail="No live releases found")


def _platform(value: str) -> str:
    if value not in VALID_PLATFORMS:
        raise HTTPException(status_code=422, detail="Invalid platform")
    return value


def _channel(value: str) -> str:
    if value not in {"stable", "beta"}:
        raise HTTPException(status_code=422, detail="Invalid channel")
    return value


def _pick_channel(releases: list[dict[str, object]], channel: str) -> dict[str, object] | None:
    return next((release for release in releases if release["channel"] == channel), None)


@router.get("/updates/latest")
async def get_latest_version(request: Request):
    try:
        release = await _stable_release(request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to fetch releases") from exc
    return {key: release[key] for key in ("version", "build_number", "download_url", "is_critical")}


@router.get("/download")
async def download_redirect(request: Request):
    try:
        release = await _stable_release(request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to fetch releases") from exc
    return RedirectResponse(_manual_download_url(release), status_code=307)


@router.get("/v2/desktop/appcast.xml")
async def get_desktop_appcast(
    request: Request,
    platform: str = Query(default="macos"),
    identity: str = Query(default="stable"),
):
    platform = _platform(platform)
    if identity not in {"stable", "beta"}:
        raise HTTPException(status_code=422, detail="Invalid identity")
    try:
        releases = await _live_releases(request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Error generating appcast") from exc
    if not releases:
        raise HTTPException(status_code=404, detail=f"No desktop releases found for platform: {platform}")
    wanted = "beta" if identity == "beta" else "stable"
    return Response(
        _appcast_xml([release for release in releases if release["channel"] == wanted], platform),
        media_type="application/xml",
        headers={"Cache-Control": "max-age=300"},
    )


@router.get("/v2/desktop/download/latest")
async def download_latest_desktop(
    request: Request,
    platform: str = "macos",
    channel: str = "stable",
    identity: str | None = None,
):
    platform = _platform(platform)
    channel = _channel(channel)
    if identity is not None and identity not in {"stable", "beta"}:
        raise HTTPException(status_code=422, detail="Invalid identity")
    if identity == "beta":
        channel = "beta"
    effective_identity = identity or channel
    try:
        releases = await _live_releases(request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to fetch releases") from exc
    if not releases:
        raise HTTPException(status_code=404, detail=f"No live desktop releases found for platform: {platform}")
    release = _pick_channel(releases, channel)
    if release is None:
        raise HTTPException(status_code=404, detail=f"No installer found for platform {platform}, channel: {channel}")
    url = _manual_download_url(release)
    return HTMLResponse(
        _download_landing_html(
            url,
            platform=platform,
            channel="beta" if effective_identity == "beta" else "stable",
            version=str(release["version"]),
        )
    )


@router.get("/v2/desktop/download/beta")
async def download_beta_desktop(request: Request, platform: str = Query(default="macos")):
    return await download_latest_desktop(request, platform=platform, channel="beta", identity="beta")


_PREVIEW_SELECT = (
    "SELECT slug, source_sha, dmg_url, dmg_sha256, app_name, bundle_id, url_scheme, built_at, signer, "
    "notarization, notes, backend_url FROM cf_desktop_preview_manifests "
)


async def _preview_from_d1(request: Request, slug: str, source_sha: str | None = None) -> dict[str, object] | None:
    env = request.scope["env"]
    resolved_sha = source_sha
    if resolved_sha is None:
        pointer = await (
            env.APP_DB.prepare("SELECT source_sha FROM cf_desktop_preview_pointers WHERE slug = ? LIMIT 1")
            .bind(slug)
            .first()
        )
        resolved_sha = _preview_source_sha(pointer.get("source_sha")) if isinstance(pointer, dict) else None
    if resolved_sha is None:
        return None
    row = (
        await env.APP_DB.prepare(_PREVIEW_SELECT + "WHERE slug = ? AND source_sha = ? LIMIT 1")
        .bind(slug, resolved_sha)
        .first()
    )
    return _preview_manifest(row, slug=slug, source_sha=resolved_sha)


def _preview_landing_html(manifest: dict[str, object]) -> str:
    app_name = html.escape(str(manifest["app_name"]), quote=True)
    slug = html.escape(str(manifest["slug"]), quote=True)
    source_sha = html.escape(str(manifest["source_sha"]), quote=True)
    built_at = html.escape(str(manifest["built_at"]), quote=True)
    dmg_url = html.escape(str(manifest["dmg_url"]), quote=True)
    notes = manifest.get("notes")
    notes_html = f'<p class="notes">{html.escape(str(notes), quote=True)}</p>' if notes else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="2;url={dmg_url}">
    <title>Download {app_name} for macOS</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0a0a0a;
               color: #f5f5f5; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
        main {{ width: min(620px, calc(100% - 48px)); padding: 40px; border: 1px solid #2a2a2a; border-radius: 16px;
               background: #121212; text-align: center; }}
        h1 {{ margin: 0 0 12px; font-size: 28px; }}
        p {{ color: #b6b6b6; line-height: 1.5; }}
        code {{ display: block; overflow-wrap: anywhere; padding: 12px; border-radius: 8px; background: #1c1c1c;
               color: #e8e8e8; font-size: 13px; }}
        a {{ color: #ffffff; }}
        .notes {{ white-space: pre-wrap; }}
        .meta {{ margin-top: 24px; text-align: left; font-size: 13px; color: #909090; }}
    </style>
</head>
<body>
    <main>
        <h1>Downloading {app_name}</h1>
        <p>Your macOS preview download should start automatically.</p>
        <p><a href="{dmg_url}">Download the preview DMG</a></p>
        {notes_html}
        <div class="meta">
            <p>Preview branch: <strong>{slug}</strong></p>
            <p>Approved source commit:</p>
            <code>{source_sha}</code>
            <p>Build time: {built_at}</p>
        </div>
    </main>
</body>
</html>"""


async def _serve_preview(request: Request, slug: str, source_sha: str | None = None) -> HTMLResponse:
    normalized_slug = _preview_slug(slug)
    normalized_sha = _preview_source_sha(source_sha) if source_sha is not None else None
    if normalized_slug is None or (source_sha is not None and normalized_sha is None):
        raise HTTPException(status_code=404, detail="Preview not found")
    try:
        manifest = await _preview_from_d1(request, normalized_slug, normalized_sha)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Preview unavailable") from exc
    if manifest is None:
        raise HTTPException(status_code=404, detail="Preview not found")
    return HTMLResponse(_preview_landing_html(manifest), headers={"Cache-Control": "no-store"})


def _preview_publish_key_valid(request: Request) -> bool:
    expected = getattr(request.scope["env"], "DESKTOP_PREVIEW_PUBLISH_KEY", None)
    provided = request.headers.get("secret-key")
    return (
        isinstance(expected, str)
        and bool(expected)
        and isinstance(provided, str)
        and hmac.compare_digest(provided, expected)
    )


async def _delist_preview_pointer(request: Request, slug: str, expected_generation: int) -> dict[str, object]:
    normalized_slug = _preview_slug(slug)
    if normalized_slug is None:
        raise HTTPException(status_code=409, detail="slug must use lowercase letters, digits, and path-safe hyphens")

    env = request.scope["env"]
    pointer = await (
        env.APP_DB.prepare("SELECT source_sha, generation FROM cf_desktop_preview_pointers WHERE slug = ? LIMIT 1")
        .bind(normalized_slug)
        .first()
    )
    if pointer is None:
        return {"slug": normalized_slug, "deleted": False, "generation": None}
    current_generation = pointer.get("generation") if isinstance(pointer, dict) else None
    if isinstance(current_generation, str) and current_generation.isdigit():
        current_generation = int(current_generation)
    if not isinstance(current_generation, int) or isinstance(current_generation, bool) or current_generation < 0:
        raise HTTPException(status_code=409, detail="preview pointer is malformed")
    if expected_generation != current_generation:
        raise HTTPException(
            status_code=409,
            detail=f"generation mismatch: expected {expected_generation}, current {current_generation}",
        )

    result = await (
        env.APP_DB.prepare("DELETE FROM cf_desktop_preview_pointers WHERE slug = ? AND generation = ?")
        .bind(normalized_slug, current_generation)
        .run()
    )
    changes = result.get("meta", {}).get("changes", 0) if isinstance(result, dict) else 0
    if int(changes or 0) == 0:
        # A concurrent compare-and-delete won the race. Re-read so callers get
        # the same conflict contract as the legacy transactional implementation.
        latest = await (
            env.APP_DB.prepare("SELECT generation FROM cf_desktop_preview_pointers WHERE slug = ? LIMIT 1")
            .bind(normalized_slug)
            .first()
        )
        latest_generation = latest.get("generation") if isinstance(latest, dict) else None
        if isinstance(latest_generation, str) and latest_generation.isdigit():
            latest_generation = int(latest_generation)
        if latest_generation is None:
            return {"slug": normalized_slug, "deleted": False, "generation": None}
        raise HTTPException(
            status_code=409,
            detail=f"generation mismatch: expected {expected_generation}, current {latest_generation}",
        )
    return {"slug": normalized_slug, "deleted": True, "generation": current_generation}


async def _publish_preview(request: Request, payload: DesktopPreviewPublishRequest) -> dict[str, object]:
    manifest = _normalize_preview_publish(payload)
    env = request.scope["env"]
    existing = await (
        env.APP_DB.prepare(_PREVIEW_SELECT + "WHERE slug = ? AND source_sha = ? LIMIT 1")
        .bind(manifest["slug"], manifest["source_sha"])
        .first()
    )
    if existing is not None:
        normalized_existing = _preview_manifest(
            existing,
            slug=str(manifest["slug"]),
            source_sha=str(manifest["source_sha"]),
        )
        if normalized_existing != manifest:
            raise HTTPException(
                status_code=409, detail="preview artifact already exists with different immutable metadata"
            )

    pointer = await (
        env.APP_DB.prepare("SELECT source_sha, generation FROM cf_desktop_preview_pointers WHERE slug = ? LIMIT 1")
        .bind(manifest["slug"])
        .first()
    )
    current_sha = pointer.get("source_sha") if isinstance(pointer, dict) else None
    current_generation = pointer.get("generation", 0) if isinstance(pointer, dict) else 0
    if isinstance(current_generation, str) and current_generation.isdigit():
        current_generation = int(current_generation)
    if (
        not isinstance(current_generation, int)
        or isinstance(current_generation, bool)
        or current_generation < 0
        or (current_sha is not None and _preview_source_sha(current_sha) is None)
    ):
        raise HTTPException(status_code=409, detail="preview pointer is malformed")
    if payload.expected_generation is not None and payload.expected_generation != current_generation:
        raise HTTPException(
            status_code=409,
            detail=f"generation mismatch: expected {payload.expected_generation}, current {current_generation}",
        )

    pointer_payload: dict[str, object] = {
        "slug": manifest["slug"],
        "source_sha": current_sha,
        "generation": current_generation,
    }
    pointer_changed = current_sha != manifest["source_sha"]
    if pointer_changed:
        pointer_payload = {
            "slug": manifest["slug"],
            "source_sha": manifest["source_sha"],
            "generation": current_generation + 1,
        }
    statements = []
    if existing is None:
        statements.append(
            env.APP_DB.prepare(
                "INSERT INTO cf_desktop_preview_manifests "
                "(slug, source_sha, dmg_url, dmg_sha256, app_name, bundle_id, url_scheme, built_at, signer, "
                "notarization, notes, backend_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, unixepoch())"
            ).bind(
                manifest["slug"],
                manifest["source_sha"],
                manifest["dmg_url"],
                manifest["dmg_sha256"],
                manifest["app_name"],
                manifest["bundle_id"],
                manifest["url_scheme"],
                manifest["built_at"],
                manifest["signer"],
                manifest["notarization"],
                manifest["notes"],
                manifest["backend_url"],
            )
        )
    if pointer_changed:
        if pointer is None:
            statements.append(
                env.APP_DB.prepare(
                    "INSERT INTO cf_desktop_preview_pointers (slug, source_sha, generation, updated_at) "
                    "VALUES (?, ?, ?, unixepoch())"
                ).bind(pointer_payload["slug"], pointer_payload["source_sha"], pointer_payload["generation"])
            )
        else:
            statements.append(
                env.APP_DB.prepare(
                    "UPDATE cf_desktop_preview_pointers SET source_sha = ?, generation = ?, updated_at = unixepoch() "
                    "WHERE slug = ? AND generation = ?"
                ).bind(
                    pointer_payload["source_sha"],
                    pointer_payload["generation"],
                    pointer_payload["slug"],
                    current_generation,
                )
            )
    if statements:
        results = await env.APP_DB.batch(statements)
        pointer_result = results[-1] if pointer_changed else None
        changes = pointer_result.get("meta", {}).get("changes", 0) if isinstance(pointer_result, dict) else 0
        if pointer_changed and int(changes or 0) != 1:
            raise HTTPException(status_code=409, detail="preview pointer changed concurrently")
    return {"manifest": manifest, "pointer": pointer_payload}


@router.get("/v2/desktop/previews/{slug}")
async def download_current_desktop_preview(request: Request, slug: str):
    """Serve the current approved preview for one branch slug."""
    return await _serve_preview(request, slug)


@router.get("/v2/desktop/previews/{slug}/{source_sha}")
async def download_immutable_desktop_preview(request: Request, slug: str, source_sha: str):
    """Serve one immutable approved preview artifact by its full source SHA."""
    return await _serve_preview(request, slug, source_sha)


@router.delete("/v2/desktop/previews/{slug}")
async def delist_desktop_preview(request: Request, slug: str, payload: DesktopPreviewDelistRequest):
    """Remove only a slug's mutable landing-page pointer, retaining artifacts."""
    if not _preview_publish_key_valid(request):
        raise HTTPException(status_code=403, detail="You are not authorized to delist desktop previews")
    return {"success": True, **await _delist_preview_pointer(request, slug, payload.expected_generation)}


@router.post("/v2/desktop/previews/publish", status_code=201)
async def publish_desktop_preview(request: Request, payload: DesktopPreviewPublishRequest):
    """Register an immutable preview artifact and advance its mutable pointer."""
    if not _preview_publish_key_valid(request):
        raise HTTPException(status_code=403, detail="You are not authorized to publish desktop previews")
    try:
        result = await _publish_preview(request, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"success": True, **result}


@router.get("/v2/desktop/update-feed/windows")
async def get_windows_update_feed(request: Request, channel: str = Query(default="stable")):
    """Return the immutable Windows electron-updater feed directory.

    Stable never falls through to beta. A beta request may use the stable feed
    while the beta slot is empty, matching the legacy Windows client contract.
    The feed URL is an explicit projection field; installer URLs are never
    guessed across platforms.
    """
    channel = _channel(channel)
    try:
        releases = await _live_releases(request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to fetch releases") from exc

    served_channel = channel
    release = _pick_channel(releases, channel)
    if release is None and channel == "beta":
        release = _pick_channel(releases, "stable")
        if release is not None:
            served_channel = "stable"
            record_fallback(
                component="other",
                from_mode="desktop_windows_update_feed_beta",
                to_mode="desktop_windows_update_feed_stable",
                reason="other",
                outcome="recovered",
            )
    feed_url = _https_url(release.get("windows_feed_url")) if release else None
    if release is None or feed_url is None:
        raise HTTPException(
            status_code=404,
            detail=f"No Windows update feed found for channel: {channel}",
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {
            "requested_channel": channel,
            "served_channel": served_channel,
            "version": release["version"],
            "feed_url": feed_url,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/v2/desktop/download/windows")
async def download_windows_desktop(request: Request, channel: str = Query(default="stable")):
    _channel(channel)
    try:
        releases = await _live_releases(request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to fetch releases") from exc
    if not releases:
        raise HTTPException(status_code=404, detail="No live desktop releases found for platform: windows")
    served_channel = channel
    release = _pick_channel(releases, channel)
    if release is None:
        served_channel = "beta" if channel == "stable" else "stable"
        release = _pick_channel(releases, served_channel)
    if release is None:
        raise HTTPException(status_code=404, detail=f"No installer found for platform windows, channel: {channel}")
    notice = (
        ""
        if served_channel == channel
        else f"No {channel} build is published right now — serving the latest {served_channel} release instead."
    )
    return HTMLResponse(
        _download_landing_html(
            _manual_download_url(release),
            platform="windows",
            channel=served_channel,
            version=str(release["version"]),
            notice=notice,
        )
    )


@router.get("/v2/desktop/update-policy")
async def get_update_policy(
    request: Request, platform: str = Query(default="macos"), current_build: int | None = Query(default=None, ge=0)
):
    if platform not in VALID_PLATFORMS:
        raise HTTPException(status_code=422, detail="Invalid platform")
    try:
        row = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT id, active, severity, maximum_build_number, latest_build_number, title, message, cta_text, "
                "download_url, can_dismiss, platforms_json FROM cf_desktop_update_policy WHERE id = 'current'"
            )
            .first()
        )
    except Exception:
        return _default_policy()
    return _policy(row if isinstance(row, dict) else None, platform=platform, current_build=current_build)


@router.post("/v2/desktop/releases", status_code=201)
async def register_desktop_release_manifest(
    request: Request,
    payload: dict[str, object],
    secret_key: str | None = Header(default=None),
):
    """Register one validated immutable desktop manifest in D1.

    This endpoint does not publish a channel pointer. Publication remains a
    separate release-pipeline operation, while the manifest itself is safe to
    retry and cannot be overwritten after insertion.
    """
    if not _release_manifest_key_valid(request):
        raise HTTPException(status_code=403, detail="You are not authorized to register desktop releases")
    try:
        manifest = await _register_release_manifest(request, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Desktop release manifest projection unavailable") from exc
    return {"success": True, "manifest": manifest}


@router.post("/v2/desktop/clear-cache")
async def clear_desktop_cache(request: Request):
    """Retain the admin cache-invalidation contract for the D1 projection.

    Cloudflare desktop feeds read immutable manifests and channel pointers from
    D1, so there is no Redis/GitHub response cache to purge. Keeping this
    endpoint in API Core lets existing release automation safely retry the
    invalidation step without reaching the legacy backend or mutating release
    state.
    """
    if not _release_manifest_key_valid(request):
        raise HTTPException(status_code=403, detail="You are not authorized to perform this action")
    return {
        "success": True,
        "message": "Desktop releases cache cleared successfully",
        "projection": "d1",
    }


@router.get("/v2/desktop/releases/{release_id}")
async def get_desktop_release_manifest(
    request: Request,
    release_id: str,
    secret_key: str | None = Header(default=None),
):
    """Return the validated immutable manifest for one release id."""
    if not _release_manifest_key_valid(request):
        raise HTTPException(status_code=403, detail="You are not authorized to read desktop releases")
    if DESKTOP_RELEASE_ID_RE.fullmatch(release_id) is None:
        raise HTTPException(status_code=404, detail="desktop release manifest not found")
    try:
        row = (
            await request.scope["env"]
            .APP_DB.prepare(
                "SELECT release_id, manifest_json, manifest_sha256 FROM cf_desktop_release_manifests "
                "WHERE release_id = ? LIMIT 1"
            )
            .bind(release_id)
            .first()
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Desktop release manifest projection unavailable") from exc
    parsed = _manifest_from_row(row, release_id)
    if parsed is None:
        if row is None:
            raise HTTPException(status_code=404, detail="desktop release manifest not found")
        raise HTTPException(status_code=503, detail="Desktop release manifest projection is invalid")
    manifest, digest = parsed
    return {"success": True, "manifest": manifest, "manifest_sha256": digest}


@router.post("/v2/desktop/channels/promote")
async def promote_desktop_channel(
    request: Request,
    payload: dict[str, object],
    secret_key: str | None = Header(default=None),
):
    """Advance the macOS Stable pointer after an immutable manifest is retained."""
    if not _release_manifest_key_valid(request):
        raise HTTPException(status_code=403, detail="You are not authorized to promote desktop channels")
    try:
        return await _promote_stable_pointer(request, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Desktop channel pointer projection unavailable") from exc
