"""Read-only GitHub evidence for the Cloudflare-owned macOS Beta fence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

try:
    from workers import fetch as worker_fetch
except ModuleNotFoundError as error:
    if error.name != "js":
        raise
    worker_fetch = None  # type: ignore[assignment]


REPOSITORY = "BasedHardware/omi"
TAG_RE = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\+(?P<build>[1-9][0-9]*)-macos$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
UTC_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$")
RELEASE_ASSET_HOST = "release-assets.githubusercontent.com"
EXPECTED_TEAM_ID = "9536L8KLMP"
STRUCTURAL_CHECKS = frozenset(
    {
        "Launch + identity metadata is aligned",
        "Auth persistence prerequisites: signing identity and Keychain-compatible entitlements are sane",
        "Backend routing config matches the declared external backend",
        "Sparkle/update metadata and authoritative ZIP artifacts are present",
        "Native helper/runtime bundle integrity passed",
        "Local storage/database package surface is present",
        "Signed desktop artifact smoke completed",
    }
)
BETA_BEHAVIORAL_CHECKS = frozenset(
    {
        "Signed app launches and remains alive",
        "Signed artifact Keychain write/read/delete canary passed",
        "Signed app relaunched for UserNotifications callback canary",
        "UserNotifications settings callback completion canary passed",
    }
)


class BetaEvidenceError(ValueError):
    """A candidate failed read-only Beta evidence validation."""


def _fail(message: str) -> None:
    raise BetaEvidenceError(message)


def _tag_parts(tag: object) -> tuple[str, str, int]:
    if not isinstance(tag, str):
        _fail("candidate tag identity is invalid")
    match = TAG_RE.fullmatch(tag)
    if match is None:
        _fail("candidate tag identity is invalid")
    return tag, match.group("version"), int(match.group("build"))


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or UTC_TIMESTAMP_RE.fullmatch(value) is None:
        _fail("candidate freshness is missing")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except (TypeError, ValueError, OverflowError):
        _fail("candidate freshness is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _fail("candidate freshness is invalid")
    return parsed.astimezone(timezone.utc)


def _fresh(published_at: object) -> str:
    published = _timestamp(published_at)
    try:
        maximum_age = int(os.getenv("BETA_CANDIDATE_MAX_AGE_SECONDS", "604800"))
    except (TypeError, ValueError):
        _fail("candidate freshness policy is unavailable")
    now = datetime.now(timezone.utc)
    if maximum_age <= 0 or now < published:
        _fail("candidate freshness is invalid")
    if now - published > timedelta(seconds=maximum_age):
        _fail("candidate release is stale")
    return str(published_at)


def _object(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        _fail(message)
    return dict(value)


def _assets(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        _fail("candidate GitHub release assets are invalid")
    result: list[dict[str, object]] = []
    for item in value:
        asset = _object(item, "candidate GitHub release assets are invalid")
        if not isinstance(asset.get("name"), str) or not asset["name"]:
            _fail("candidate GitHub release assets are invalid")
        if not isinstance(asset.get("browser_download_url"), str) or not asset["browser_download_url"]:
            _fail("candidate GitHub release assets are invalid")
        digest = asset.get("digest")
        if digest is not None and (not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None):
            _fail("candidate asset is missing its GitHub SHA-256 digest")
        result.append(asset)
    return result


def _asset(assets: list[dict[str, object]], name: str) -> dict[str, object]:
    matches = [asset for asset in assets if asset.get("name") == name]
    if len(matches) != 1:
        _fail("candidate is missing a canonical asset")
    return matches[0]


def _asset_url(asset: dict[str, object], tag: str, name: str) -> str:
    value = asset.get("browser_download_url")
    expected = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    encoded = f"https://github.com/{REPOSITORY}/releases/download/{tag.replace('+', '%2B')}/{name}"
    if not isinstance(value, str) or value not in {expected, encoded}:
        _fail("candidate asset identity does not match its immutable release")
    return value


def _asset_digest(asset: dict[str, object]) -> str:
    digest = asset.get("digest")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        _fail("candidate asset is missing its GitHub SHA-256 digest")
    return digest


def _metadata(body: object) -> dict[str, object]:
    if not isinstance(body, str):
        return {}
    match = re.search(r"<!-- KEY_VALUE_START\s*(.*?)\s*KEY_VALUE_END -->", body, re.DOTALL)
    if match is None:
        return {}
    values: dict[str, object] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.strip().partition(":")
        if not separator:
            continue
        key, value = key.strip(), value.strip()
        if key == "changelog":
            values[key] = [item.strip() for item in value.split("|") if item.strip()]
        else:
            values[key] = value
    return values


def _headers(env: object) -> dict[str, str]:
    token = getattr(env, "GITHUB_TOKEN", None)
    if not isinstance(token, str) or not token:
        _fail("candidate GitHub read authorization is unavailable")
    return {
        "accept": "application/vnd.github+json",
        "authorization": f"Bearer {token}",
        "user-agent": "omi-cloudflare-worker/0.1",
        "x-github-api-version": "2022-11-28",
    }


async def _json_get(env: object, path: str) -> dict[str, object]:
    if worker_fetch is None:
        _fail("candidate GitHub read dependency is unavailable")
    try:
        response = await worker_fetch(
            f"https://api.github.com/repos/{REPOSITORY}/{path}", method="GET", headers=_headers(env)
        )
        if int(getattr(response, "status", 0)) != 200:
            _fail("candidate GitHub evidence is unavailable")
        payload = await response.json()
    except BetaEvidenceError:
        raise
    except Exception as exc:
        raise BetaEvidenceError("candidate GitHub evidence is unavailable") from exc
    return _object(payload, "candidate GitHub evidence is invalid")


async def _download(env: object, url: str) -> bytes:
    if worker_fetch is None:
        _fail("candidate GitHub read dependency is unavailable")
    try:
        response = await worker_fetch(url, method="GET", headers=_headers(env))
        status = int(getattr(response, "status", 0))
        if status in {301, 302, 303, 307, 308}:
            location = getattr(response, "headers", {}).get("location")
            parsed = urlsplit(location) if isinstance(location, str) else None
            if (
                parsed is None
                or parsed.scheme != "https"
                or parsed.hostname != RELEASE_ASSET_HOST
                or parsed.port is not None
                or parsed.username is not None
                or parsed.password is not None
            ):
                _fail("candidate GitHub asset is unavailable")
            response = await worker_fetch(location, method="GET", headers=_headers(env))
            status = int(getattr(response, "status", 0))
        if status != 200:
            _fail("candidate GitHub asset is unavailable")
        array_buffer = getattr(response, "arrayBuffer", None)
        if not callable(array_buffer):
            _fail("candidate GitHub asset reader is unavailable")
        return bytes(await array_buffer())
    except BetaEvidenceError:
        raise
    except Exception as exc:
        raise BetaEvidenceError("candidate GitHub asset is unavailable") from exc


async def _source_sha(env: object, tag: str) -> str:
    ref = await _json_get(env, f"git/ref/tags/{quote(tag, safe='')}")
    target = _object(ref.get("object"), "candidate tag is invalid")
    object_type, sha = target.get("type"), target.get("sha")
    if object_type not in {"commit", "tag"} or not isinstance(sha, str) or not sha:
        _fail("candidate tag is invalid")
    if object_type == "tag":
        nested = _object(
            (await _json_get(env, f"git/tags/{quote(sha, safe='')}")).get("object"), "candidate tag is invalid"
        )
        if nested.get("type") != "commit" or not isinstance(nested.get("sha"), str):
            _fail("candidate tag is invalid")
        sha = nested["sha"]
    comparison = await _json_get(env, f"compare/{quote(sha, safe='')}...main")
    if comparison.get("status") not in {"ahead", "identical"}:
        _fail("candidate source identity is not merged main")
    return sha


def _smoke(
    payload: bytes,
    *,
    tag: str,
    source_sha: str,
    bundle_id: str,
    version: str,
    build: str,
    expected_artifacts: dict[str, str],
    behavioral: bool,
    label: str,
) -> None:
    try:
        smoke = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError):
        _fail(f"{label} signed-artifact smoke is invalid")
    if not isinstance(smoke, dict):
        _fail(f"{label} signed-artifact smoke is invalid")
    required = {
        "ok": True,
        "release_tag": tag,
        "expected_channel": "beta",
        "bundle_id": bundle_id,
        "version": version,
        "build": build,
        "team_id": EXPECTED_TEAM_ID,
        "source_sha": source_sha,
    }
    if any(smoke.get(key) != value for key, value in required.items()):
        _fail(f"{label} signed-artifact smoke does not bind the target")
    checks = smoke.get("checks")
    required_checks = set(STRUCTURAL_CHECKS)
    if behavioral:
        required_checks.update(BETA_BEHAVIORAL_CHECKS)
    if not isinstance(checks, list) or any(not isinstance(check, str) for check in checks):
        _fail(f"{label} signed-artifact smoke is incomplete")
    if not required_checks.issubset(checks):
        _fail(f"{label} signed-artifact smoke is incomplete")
    if behavioral:
        callback = smoke.get("notification_callback_canary")
        expected_callback = {
            "schema": 1,
            "event": "user-notifications-settings-callback-completed",
            "bundle_id": bundle_id,
            "main_actor": True,
            "validated": True,
        }
        if not isinstance(callback, dict) or any(
            callback.get(key) != value for key, value in expected_callback.items()
        ):
            _fail(f"{label} UserNotifications callback canary is invalid")
        if not isinstance(callback.get("authorization_status"), int):
            _fail(f"{label} UserNotifications callback canary is incomplete")
    artifacts = smoke.get("artifacts")
    if not isinstance(artifacts, list):
        _fail(f"{label} smoke has no artifact digest set")
    observed: dict[str, str] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            _fail(f"{label} smoke artifact digest set is invalid")
        key, digest = artifact.get("label"), artifact.get("sha256")
        if key in expected_artifacts and isinstance(digest, str):
            observed[key] = f"sha256:{digest}"
    if observed != expected_artifacts:
        _fail(f"{label} smoke does not bind the published artifacts")


def _manifest(
    *,
    tag: str,
    version: str,
    build: int,
    source_sha: str,
    published_at: str,
    zip_url: str,
    zip_digest: str,
    dmg_url: str,
    dmg_digest: str,
    smoke_url: str,
    smoke_digest: str,
    signature: str,
    qualification_tier: str,
    qualification_passed: bool,
    changelog: list[str],
    mandatory: bool,
) -> dict[str, object]:
    from desktop_release_routes import _validate_desktop_manifest

    value: dict[str, object] = {
        "schema_version": 1,
        "release_id": tag,
        "platform": "macos",
        "version": version,
        "build_number": build,
        "app_source_sha": source_sha,
        "zip_url": zip_url,
        "zip_sha256": zip_digest,
        "dmg_url": dmg_url,
        "dmg_sha256": dmg_digest,
        "ed_signature": signature,
        "qualification_evidence_asset": smoke_url.rsplit("/", 1)[-1],
        "qualification_evidence_sha256": smoke_digest,
        "qualification_tier": qualification_tier,
        "qualification_passed": qualification_passed,
        "backend_mode": "app_only",
        "compatibility_contract": {
            "schema_version": 1,
            "app_release_id": tag,
            "app_version": version,
            "app_build_number": build,
            "backend_mode": "app_only",
            "environment_contract_version": "desktop-backend-env-v1",
        },
        "environment_contract_version": "desktop-backend-env-v1",
        "created_at": published_at,
        "published_at": published_at,
        "changelog": changelog,
        "mandatory": mandatory,
    }
    try:
        return _validate_desktop_manifest(value)
    except ValueError as exc:
        raise BetaEvidenceError("candidate manifest is invalid") from exc


async def build_signed_beta_manifest(env: object, tag: str) -> dict[str, object]:
    tag, version, build = _tag_parts(tag)
    release = await _json_get(env, f"releases/tags/{quote(tag, safe='')}")
    if release.get("tag_name") != tag or release.get("draft") is not False or release.get("prerelease") is not False:
        _fail("candidate is not an immutable published release")
    published_at = _fresh(release.get("published_at"))
    metadata = _metadata(release.get("body"))
    if metadata.get("channel") != "candidate" or str(metadata.get("isLive", "")).lower() != "false":
        _fail("candidate release metadata is not non-live candidate state")
    source_sha = await _source_sha(env, tag)
    assets = _assets(release.get("assets"))
    selected = {
        name: _asset(assets, name)
        for name in (
            "Omi.zip",
            "omi.dmg",
            "Omi.Beta.zip",
            "omi-beta.dmg",
            "desktop-smoke-result.json",
            "desktop-smoke-result-beta.json",
        )
    }
    urls = {name: _asset_url(asset, tag, name) for name, asset in selected.items()}
    digests = {name: _asset_digest(asset) for name, asset in selected.items()}
    stable_smoke, beta_smoke = await _download(env, urls["desktop-smoke-result.json"]), await _download(
        env, urls["desktop-smoke-result-beta.json"]
    )
    if (
        f"sha256:{hashlib.sha256(stable_smoke).hexdigest()}" != digests["desktop-smoke-result.json"]
        or f"sha256:{hashlib.sha256(beta_smoke).hexdigest()}" != digests["desktop-smoke-result-beta.json"]
    ):
        _fail("candidate signed-smoke digest does not match its immutable release asset")
    _smoke(
        stable_smoke,
        tag=tag,
        source_sha=source_sha,
        bundle_id="com.omi.computer-macos",
        version=version,
        build=str(build),
        expected_artifacts={"sparkle_zip": digests["Omi.zip"], "dmg": digests["omi.dmg"]},
        behavioral=False,
        label="candidate stable identity",
    )
    _smoke(
        beta_smoke,
        tag=tag,
        source_sha=source_sha,
        bundle_id="com.omi.computer-macos.beta",
        version=version,
        build=str(build),
        expected_artifacts={"sparkle_zip": digests["Omi.Beta.zip"], "dmg": digests["omi-beta.dmg"]},
        behavioral=True,
        label="candidate",
    )
    signature = str(metadata.get("edSignature") or "").strip()
    if not signature or not str(metadata.get("betaEdSignature") or "").strip():
        _fail("candidate has no complete Sparkle signatures")
    changelog = metadata.get("changelog")
    return _manifest(
        tag=tag,
        version=version,
        build=build,
        source_sha=source_sha,
        published_at=published_at,
        zip_url=urls["Omi.zip"],
        zip_digest=digests["Omi.zip"],
        dmg_url=urls["omi.dmg"],
        dmg_digest=digests["omi.dmg"],
        smoke_url=urls["desktop-smoke-result-beta.json"],
        smoke_digest=digests["desktop-smoke-result-beta.json"],
        signature=signature,
        qualification_tier="signed-smoke",
        qualification_passed=False,
        changelog=changelog if isinstance(changelog, list) and all(isinstance(item, str) for item in changelog) else [],
        mandatory=str(metadata.get("mandatory", "false")).lower() in {"true", "1", "yes"},
    )


async def build_emergency_beta_manifest(env: object, tag: str) -> dict[str, object]:
    tag, version, build = _tag_parts(tag)
    release = await _json_get(env, f"releases/tags/{quote(tag, safe='')}")
    if release.get("tag_name") != tag or release.get("draft") is not False or release.get("prerelease") is not False:
        _fail("emergency target is not an immutable published release")
    published_at = _fresh(release.get("published_at"))
    source_sha = await _source_sha(env, tag)
    assets = _assets(release.get("assets"))
    selected = {name: _asset(assets, name) for name in ("Omi.zip", "omi.dmg", "desktop-smoke-result.json")}
    urls = {name: _asset_url(asset, tag, name) for name, asset in selected.items()}
    digests = {name: _asset_digest(asset) for name, asset in selected.items()}
    smoke = await _download(env, urls["desktop-smoke-result.json"])
    if f"sha256:{hashlib.sha256(smoke).hexdigest()}" != digests["desktop-smoke-result.json"]:
        _fail("emergency target GitHub digests do not match immutable assets")
    _smoke(
        smoke,
        tag=tag,
        source_sha=source_sha,
        bundle_id="com.omi.computer-macos",
        version=version,
        build=str(build),
        expected_artifacts={"sparkle_zip": digests["Omi.zip"], "dmg": digests["omi.dmg"]},
        behavioral=False,
        label="emergency target",
    )
    signature = str(_metadata(release.get("body")).get("edSignature") or "").strip()
    if not signature:
        _fail("emergency target has no Sparkle signature")
    return _manifest(
        tag=tag,
        version=version,
        build=build,
        source_sha=source_sha,
        published_at=published_at,
        zip_url=urls["Omi.zip"],
        zip_digest=digests["Omi.zip"],
        dmg_url=urls["omi.dmg"],
        dmg_digest=digests["omi.dmg"],
        smoke_url=urls["desktop-smoke-result.json"],
        smoke_digest=digests["desktop-smoke-result.json"],
        signature=signature,
        qualification_tier="emergency",
        qualification_passed=False,
        changelog=[],
        mandatory=False,
    )


__all__ = ["BetaEvidenceError", "build_emergency_beta_manifest", "build_signed_beta_manifest"]
