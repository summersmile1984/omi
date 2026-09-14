#!/usr/bin/env python3
"""List every fork source-owner whose recorded digest no longer matches upstream.

Both client stages verify their replacement contracts while they build and stop at
the first mismatch:

    desktop/macos/fork/swift_overlay.py:  OverlayError: Swift owner changed and requires review: ...
    app/fork/prepare.py:                  ValueError(f"Upstream source owner changed; review required: {name}")

So one upstream import that invalidates several owners costs one slow native/Flutter
build per owner, and the reviewer never sees the whole list before starting. The
v0.12.348 import invalidated eight contracts across three components (one backend
patch binding, two desktop Swift owners, five Flutter file owners); this audit turns
that into a single pre-flight list.

Registry semantics:
  * app/fork/source-owners.json          -> {"base": <reviewed revision>, "files": {path: sha256}}
  * desktop/macos/fork/source-owners.json -> {key: sha256}, where key is a whole file
    ("AppBuild.swift"), a Swift declaration ("AuthService.swift:configure()"), or a
    marker-bounded region owned by desktop/macos/fork/prepare.py.

Exit status is 1 when any owner is stale or unresolvable, 0 otherwise. Re-recording a
digest is the *last* step: review what upstream changed in that owner first, and fix
the fork replacement when the change is semantic (the same import additionally made
AuthService.configure() drop upstream's restoring-phase watchdog, which is a code fix,
not a digest re-record).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FLUTTER_REGISTRY = ROOT / "app/fork/source-owners.json"
DESKTOP_REGISTRY = ROOT / "desktop/macos/fork/source-owners.json"
DESKTOP_SOURCES = ROOT / "desktop/macos/Desktop/Sources"


def audit_flutter() -> tuple[list[str], list[str], int]:
    registry = json.loads(FLUTTER_REGISTRY.read_text(encoding="utf-8"))
    files = registry.get("files", {})
    stale, unresolved = [], []
    for name, digest in sorted(files.items()):
        path = ROOT / "app" / name
        if not path.is_file():
            unresolved.append(f"{name} (missing)")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            stale.append(name)
    return stale, unresolved, len(files)


def _desktop_regions() -> dict[str, tuple[str, str]]:
    """Marker-bounded owners, read from the module that owns the markers."""
    sys.path.insert(0, str(ROOT / "desktop/macos/fork"))
    import prepare  # noqa: PLC0415 - imported here so the audit runs without macOS tooling

    return {"OmiApp.swift:firebase-bootstrap": (prepare.FIREBASE_START, prepare.FIREBASE_END)}


def _desktop_path(name: str) -> Path | None:
    direct = DESKTOP_SOURCES / name
    if direct.is_file():
        return direct
    hits = sorted(DESKTOP_SOURCES.rglob(Path(name).name))
    return hits[0] if len(hits) == 1 else None


def audit_desktop() -> tuple[list[str], list[str], int, str]:
    registry = json.loads(DESKTOP_REGISTRY.read_text(encoding="utf-8"))
    if shutil.which("xcrun") is None:
        return [], [], len(registry), "skipped: Swift declaration spans need xcrun (macOS)"

    sys.path.insert(0, str(ROOT / "desktop/macos/fork"))
    from swift_overlay import declarations  # noqa: PLC0415

    regions = _desktop_regions()
    stale, unresolved = [], []
    for key, digest in sorted(registry.items()):
        name = key.split(":", 1)[0]
        path = _desktop_path(name)
        if path is None:
            unresolved.append(f"{key} (file not found)")
            continue
        # Whole-file owners include binaries (brand rasters); only the region and
        # declaration owners need decoded text.
        if key not in regions and ":" not in key:
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                stale.append(key)
            continue
        text = path.read_text(encoding="utf-8")
        if key in regions:
            start_marker, end_marker = regions[key]
            if text.count(start_marker) != 1 or text.count(end_marker) != 1:
                stale.append(key)
                continue
            left = text.index(start_marker)
            right = text.index(end_marker, left)
            actual = hashlib.sha256(text[left:right].encode()).hexdigest()
        elif ":" in key:
            signature = key.split(":", 1)[1]
            spans = declarations(path).get(signature, [])
            if len(spans) != 1:
                stale.append(key)
                continue
            start, end = spans[0]
            actual = hashlib.sha256(path.read_bytes()[start:end]).hexdigest()
        if actual != digest:
            stale.append(key)
    return stale, unresolved, len(registry), "audited"


def main() -> int:
    flutter_stale, flutter_unresolved, flutter_total = audit_flutter()
    desktop_stale, desktop_unresolved, desktop_total, desktop_note = audit_desktop()

    print(f"fork overlay owners: flutter {flutter_total} ({len(flutter_stale)} stale), "
          f"desktop {desktop_total} ({len(desktop_stale)} stale) — {desktop_note}")
    for label, entries in (("stale", flutter_stale + desktop_stale), ("unresolved", flutter_unresolved + desktop_unresolved)):
        for entry in entries:
            print(f"  {label}: {entry}", file=sys.stderr)
    if flutter_stale or desktop_stale or flutter_unresolved or desktop_unresolved:
        print(
            "Review what upstream changed in each owner, fix the fork replacement when the change is "
            "semantic, then re-record the digest in the matching source-owners.json.",
            file=sys.stderr,
        )
        return 1
    print("every recorded owner still matches upstream.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
