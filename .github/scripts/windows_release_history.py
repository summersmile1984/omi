#!/usr/bin/env python3
"""Validate a bounded, de-identified Windows GitHub release export.

The Windows workflow publishes a ``v<version>-windows`` GitHub release with a
versioned NSIS installer, its blockmap, and ``latest.yml``.  This module is a
read-only operator boundary for preserving that history before any future
Cloudflare projection exists.  It deliberately does not call GitHub, copy an
artifact, write D1/R2, or promote a channel.

The input is an operator-created JSON export, not a GitHub API response.  A
separate source fingerprint binds the export to the release snapshot from
which it was produced.  The emitted plan has no timestamps, credentials, or
machine paths, so identical valid exports produce identical bytes and plan
hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, NoReturn
from urllib.parse import unquote, urlsplit


SCHEMA_VERSION = 1
MAX_EXPORT_BYTES = 256 * 1024
MAX_PLAN_BYTES = 256 * 1024
MAX_TEXT_BYTES = 4 * 1024
MAX_RELEASE_ID_BYTES = 128
MAX_ASSET_URL_BYTES = 2_048
MAX_FINGERPRINT_BYTES = 64
REPOSITORY = "BasedHardware/omi"
RELEASE_ID_RE = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-windows$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BUILD_RE = re.compile(r"^[1-9][0-9]*$")
ASSET_NAMES = {
    "exe": "Omi-for-Windows-Setup-{version}.exe",
    "blockmap": "Omi-for-Windows-Setup-{version}.exe.blockmap",
    "latest_yml": "latest.yml",
}

# Export JSON is de-identified, but reject accidental credential-bearing
# material even when it is nested in a future extension field.  The allow-list
# below remains the primary schema boundary; this scan makes failures safer
# when an operator prepares an export with an unexpected object shape.
SECRET_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|credential|authorization|private[_-]?key|"
    r"access[_-]?key|client[_-]?secret|api[_-]?key|cookie|session)",
    re.IGNORECASE,
)
SECRET_VALUE_RE = re.compile(
    r"(?:-----BEGIN [^-]*PRIVATE KEY-----|Bearer\s+\S+|gh[pousr]_[A-Za-z0-9_]+|"
    r"github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,})",
    re.IGNORECASE,
)


class WindowsReleaseHistoryError(ValueError):
    """The Windows release export is unsafe or violates the v1 contract."""


def _fail(message: str) -> NoReturn:
    raise WindowsReleaseHistoryError(message)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    return value


def _bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def _text(value: object, label: str, *, maximum: int = MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(f"{label} must be a non-empty string without surrounding whitespace")
    if "\x00" in value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        _fail(f"{label} contains a control character")
    if _bytes(value) > maximum:
        _fail(f"{label} exceeds {maximum} UTF-8 bytes")
    return value


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        _fail(f"{label} must be a positive integer")
    return value


def _sha256(value: object, label: str) -> str:
    text = _text(value, label, maximum=MAX_FINGERPRINT_BYTES)
    if not SHA256_RE.fullmatch(text):
        _fail(f"{label} must be 64 lowercase hexadecimal characters")
    return text


def _check_for_secrets(value: object, path: str = "export") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                _fail(f"{path} contains a non-string key")
            if SECRET_KEY_RE.search(key):
                _fail(f"{path}.{key} looks like a credential field")
            _check_for_secrets(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _check_for_secrets(nested, f"{path}[{index}]")
    elif isinstance(value, str) and SECRET_VALUE_RE.search(value):
        _fail(f"{path} contains credential-like material")


def _require_exact_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail(f"{label} contains unsupported field(s): {', '.join(unknown)}")


def _github_asset_url(value: object, *, release_id: str, version: str, asset: str) -> str:
    url = _text(value, f"assets.{asset}.url", maximum=MAX_ASSET_URL_BYTES)
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise WindowsReleaseHistoryError(f"assets.{asset}.url is not a valid URL") from exc
    try:
        port = parsed.port
    except ValueError as exc:
        raise WindowsReleaseHistoryError(f"assets.{asset}.url has an invalid port") from exc

    expected_name = ASSET_NAMES[asset].format(version=version)
    expected_path = f"/BasedHardware/omi/releases/download/{release_id}/{expected_name}"
    # urlsplit().hostname normalizes case, but the repository/path is kept
    # case-sensitive so an alternate GitHub owner/repository cannot be smuggled
    # through the export.  Explicitly reject encoded path separators/traversal;
    # comparing the decoded path alone would otherwise make those ambiguous.
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or port is not None
        or "%" in parsed.path
        or unquote(parsed.path) != expected_path
        or "/../" in parsed.path
        or "/./" in parsed.path
    ):
        _fail(f"assets.{asset}.url must be the clean GitHub release URL for {expected_name}")
    return url


def _validate_source(value: object, *, release_id: str) -> dict[str, str]:
    source = _object(value, "source")
    _require_exact_keys(source, {"kind", "repository", "release_id", "release_fingerprint"}, "source")
    kind = _text(source.get("kind"), "source.kind")
    repository = _text(source.get("repository"), "source.repository", maximum=256)
    source_release_id = _text(source.get("release_id"), "source.release_id", maximum=MAX_RELEASE_ID_BYTES)
    if kind != "github-release" or repository != REPOSITORY:
        _fail("source must identify the BasedHardware/omi GitHub release")
    if source_release_id != release_id:
        _fail("source.release_id must match release.release_id")
    return {
        "kind": kind,
        "repository": repository,
        "release_id": source_release_id,
        "release_fingerprint": _sha256(source.get("release_fingerprint"), "source.release_fingerprint"),
    }


def _validate_release(value: object) -> dict[str, Any]:
    release = _object(value, "release")
    _require_exact_keys(release, {"release_id", "version", "build_number", "prerelease", "channel", "assets"}, "release")
    release_id = _text(release.get("release_id"), "release.release_id", maximum=MAX_RELEASE_ID_BYTES)
    match = RELEASE_ID_RE.fullmatch(release_id)
    if not match:
        _fail("release.release_id must use v<semver>-windows form")
    version = _text(release.get("version"), "release.version", maximum=64)
    if not VERSION_RE.fullmatch(version) or version != match.group("version"):
        _fail("release.version must exactly match release.release_id")
    build_number = _positive_int(release.get("build_number"), "release.build_number")
    prerelease = release.get("prerelease")
    if not isinstance(prerelease, bool):
        _fail("release.prerelease must be a boolean")
    channel = release.get("channel")
    if channel not in {"beta", "stable"}:
        _fail("release.channel must be beta or stable")
    if (prerelease and channel != "beta") or (not prerelease and channel != "stable"):
        _fail("release.channel must agree with release.prerelease")

    assets = _object(release.get("assets"), "release.assets")
    _require_exact_keys(assets, set(ASSET_NAMES), "release.assets")
    normalized_assets: dict[str, dict[str, Any]] = {}
    for asset in ("exe", "blockmap", "latest_yml"):
        metadata = _object(assets.get(asset), f"release.assets.{asset}")
        _require_exact_keys(metadata, {"url", "sha256"}, f"release.assets.{asset}")
        normalized_assets[asset] = {
            "url": _github_asset_url(metadata.get("url"), release_id=release_id, version=version, asset=asset),
            "sha256": _sha256(metadata.get("sha256"), f"release.assets.{asset}.sha256"),
        }
    return {
        "release_id": release_id,
        "version": version,
        "build_number": build_number,
        "prerelease": prerelease,
        "channel": channel,
        "assets": normalized_assets,
    }


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WindowsReleaseHistoryError("export contains unsupported JSON values") from exc


def validate_export(value: object) -> dict[str, Any]:
    """Validate and normalize one Windows release export."""
    export = _object(value, "export")
    _check_for_secrets(export)
    _require_exact_keys(export, {"schema_version", "source", "release"}, "export")
    if export.get("schema_version") != SCHEMA_VERSION:
        _fail("schema_version must be 1")
    release = _validate_release(export.get("release"))
    source = _validate_source(export.get("source"), release_id=release["release_id"])
    normalized = {"schema_version": SCHEMA_VERSION, "source": source, "release": release}
    if len(_canonical(normalized)) > MAX_EXPORT_BYTES:
        _fail(f"normalized export exceeds {MAX_EXPORT_BYTES} bytes")
    return normalized


def build_dry_run_plan(value: object) -> dict[str, Any]:
    """Return a deterministic reviewed plan without external side effects."""
    export = validate_export(value)
    source = export["source"]
    release = export["release"]
    plan_without_hash = {
        "mode": "dry-run",
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "release": release,
        "action": "stage",
        "status": "planned",
    }
    plan_hash = hashlib.sha256(_canonical(plan_without_hash)).hexdigest()
    plan = {**plan_without_hash, "plan_hash": plan_hash}
    if len(_canonical(plan)) > MAX_PLAN_BYTES:
        _fail(f"review plan exceeds {MAX_PLAN_BYTES} bytes")
    return plan


def load_export(path: str) -> object:
    """Read one bounded UTF-8 JSON export from a file or stdin."""
    if path == "-":
        raw = sys.stdin.buffer.read(MAX_EXPORT_BYTES + 1)
    else:
        # Do not use Path.read_bytes here: it loads an untrusted operator file
        # in full before the size check, defeating the memory bound.
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_EXPORT_BYTES + 1)
    if not raw or len(raw) > MAX_EXPORT_BYTES:
        _fail(f"export must be between 1 and {MAX_EXPORT_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WindowsReleaseHistoryError("export must be valid UTF-8 JSON") from exc


def write_plan(plan: dict[str, Any], output: str | None) -> None:
    payload = _canonical(plan) + b"\n"
    if output is None or output == "-":
        sys.stdout.buffer.write(payload)
        return
    Path(output).write_bytes(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", required=True, help="bounded JSON export path, or - for stdin")
    parser.add_argument("--output", help="write the deterministic dry-run plan here (default: stdout)")
    args = parser.parse_args(argv)
    try:
        plan = build_dry_run_plan(load_export(args.export))
        write_plan(plan, args.output)
    except (OSError, WindowsReleaseHistoryError) as exc:
        print(f"Windows release history verification FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
