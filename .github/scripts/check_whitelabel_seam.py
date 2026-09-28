#!/usr/bin/env python3
"""Enforce the WHITELABEL-MODEL six-declared-files seam.

The fork constitution (dev/unified-main/WHITELABEL-MODEL.md) declares six
paths as the only ones fork-owned code may write to main. This check scans
the diff between the base ref and HEAD and fails on any changed path that
looks like brand/white-label work but is NOT one of those six.

The category globs match what scripts/brand/apply.py itself reads and writes
(its generators live under scripts/brand/generators/, manifests under
brand/<id>/, fixtures and resources under brand/_allow.yaml and
scripts/brand/raster/, profile generator under scripts/profiles/) so any
real brand work either lives in those existing fork-owned paths or extends
the 6 declared seams. Touching anything else to add brand behaviour is the
anti-pattern this guard is here to catch.

Pattern: a changed path matches this guard when it lies under at least one
of the brand-globs AND is not in the six-declared-files list AND is not
an obvious non-product path (docs, CI workflows, fork-owned identity
directories whose names match the apply-style prepare/stage scripts the
model endorses).

Usage:
  python3 .github/scripts/check_whitelabel_seam.py --base origin/main
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Paths fork-owned code may write to main per WHITELABEL-MODEL.md §2.
DECLARED_SEAMS = frozenset({
    "app/lib/flavors.brand.dart",
    "app/lib/env/fork/deployment_profiles.g.dart",
    "backend/fork/firmware_brand.generated.json",
    "backend/fork/deployment_profiles.generated.json",
    "desktop/macos/Desktop/Sources/Generated/DeploymentProfiles.generated.swift",
    "web/app/src/lib/fork/deploymentProfile.generated.ts",
})

# Glob prefixes whose paths are recognised brand/white-label work. A changed
# path under any of these globs must either be a declared seam or a path
# the model endorses (fork-owned identity directories, profile generator
# inputs, raster fixtures). Anything else fails.
BRAND_GLOBS: tuple[str, ...] = (
    "brand/",
    "scripts/brand/",
    "scripts/brand/generators/",
    "scripts/profiles/",
    "deploy/profiles/",
    "deploy/web/",
    "backend/fork/firmware.py",
    "backend/fork/firmware_brand.generated.json",
    "backend/fork/brand_transport.py",
    "backend/fork/patches/",
    "app/fork/",
    "app/fork/identity/",
    "app/fork/overlays/",
    "app/fork/tests/",
    "app/lib/flavors.brand.dart",
    "app/lib/flavors.dart",
    "desktop/macos/fork/",
    "desktop/windows/fork/",
    "web/app/fork/",
    "omi/firmware/fork/",
)

# Paths that look like brand work but the model explicitly endorses.
# Keep this list tight: any addition should cite WHITELABEL-MODEL.md and
# identify which of the four patterns (prepare / shim / stage / render)
# the path belongs to.
ENDORSED_PATHS: frozenset[str] = frozenset({
    "app/lib/flavors.brand.dart",
    "app/lib/env/fork/deployment_profiles.g.dart",
    "backend/fork/firmware_brand.generated.json",
    "backend/fork/deployment_profiles.generated.json",
    "desktop/macos/Desktop/Sources/Generated/DeploymentProfiles.generated.swift",
    "web/app/src/lib/fork/deploymentProfile.generated.ts",
})

# Docs/CI/meta paths: a brand-glob path that lands here is part of the
# model (constitution doc, manifest file that defines brand behaviour) and
# is allowed.
META_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^brand/_schema/"),
    re.compile(r"^brand/(?:omi-upstream|eddy)/manifest\.yaml$"),
    re.compile(r"^brand/(?:omi-upstream|eddy)/assets/"),
    re.compile(r"^brand/_allow\.yaml$"),
    re.compile(r"^scripts/brand/test_[^/]+\.py$"),
    re.compile(r"^scripts/brand/schema_validate\.py$"),
    re.compile(r"^scripts/brand/manifest\.py$"),
    re.compile(r"^scripts/brand/yaml_lite\.py$"),
    re.compile(r"^scripts/brand/check\.py$"),
    re.compile(r"^scripts/brand/apply\.py$"),
    re.compile(r"^scripts/brand/generators/"),
    re.compile(r"^scripts/brand/lexicon\.yaml$"),
    re.compile(r"^scripts/profiles/check_tables\.py$"),
    re.compile(r"^scripts/profiles/test_profiles\.py$"),
    re.compile(r"^scripts/profiles/render\.py$"),
    re.compile(r"^scripts/brand/raster/"),
    re.compile(r"^deploy/profiles/"),
    re.compile(r"^omi/firmware/fork/stage\.py$"),
    re.compile(r"^omi/firmware/fork/README\.md$"),
    re.compile(r"^dev/unified-main/WHITELABEL-MODEL\.md$"),
    re.compile(r"^dev/unified-main/04-brand-layer\.md$"),
    re.compile(r"^dev/unified-main/audit-2026-09-04/03-whitelabel-action-plan\.md$"),
    re.compile(r"^app/fork/prepare\.py$"),
    re.compile(r"^app/fork/dart_overlay\.dart$"),
    re.compile(r"^app/fork/source-owners\.json$"),
    re.compile(r"^app/fork/assets\.mjs$"),
    re.compile(r"^app/fork/fixture\.py$"),
    re.compile(r"^app/fork/test\.sh$"),
    re.compile(r"^app/fork/test_stage\.py$"),
    re.compile(r"^app/fork/identity/"),
    re.compile(r"^app/fork/overlays/"),
    re.compile(r"^app/fork/tests/"),
    re.compile(r"^desktop/windows/fork/prepare\.py$"),
    re.compile(r"^desktop/windows/fork/brand-stage\.mjs$"),
    re.compile(r"^desktop/windows/fork/brand-text\.json$"),
    re.compile(r"^desktop/windows/fork/assets-stage\.mjs$"),
    re.compile(r"^desktop/windows/fork/source-stage\.mjs$"),
    re.compile(r"^desktop/windows/fork/source-owners\.json$"),
    # Pattern A: desktop/macos prepare + shim scripts (declarative config generators).
    re.compile(r"^desktop/macos/fork/"),
    # Pattern D: web/app prepare + shim scripts (vite resolve.alias surface).
    re.compile(r"^web/app/fork/"),
    # Pattern D adjunct: deploy/web is the Cloudflare renderer's fork-owned
    # overlay root; changes here alter what gets rendered into the deploy
    # directory and never edit upstream Cloudflare config files.
    re.compile(r"^deploy/web/"),
    re.compile(r"^backend/fork/bootstrap\.py$"),
    re.compile(r"^backend/fork/registry\.py$"),
    re.compile(r"^backend/fork/main\.py$"),
    re.compile(r"^backend/fork/profile\.py$"),
    re.compile(r"^backend/fork/brand_transport\.py$"),
    re.compile(r"^backend/fork/firmware\.py$"),
    re.compile(r"^backend/fork/patches/"),
    re.compile(r"^scripts/fork/preflight$"),
    re.compile(r"^scripts/fork/test_(whitelabel|brand)_"),
    re.compile(r"^.github/scripts/check_whitelabel_seam\.py$"),
    re.compile(r"^.github/checks-manifest\.fork\.yaml$"),
)


def changed_paths(base: str, head: str) -> list[str]:
    """Return the list of paths changed between base and head."""
    output = subprocess.check_output(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        text=True,
        stderr=subprocess.PIPE,
    )
    return [line.strip() for line in output.splitlines() if line.strip()]


def is_brand_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in BRAND_GLOBS)


def is_endorsed(path: str) -> bool:
    if path in ENDORSED_PATHS:
        return True
    if path in DECLARED_SEAMS:
        return True
    return any(pattern.match(path) for pattern in META_PATH_PATTERNS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/main", help="base ref to diff against (default: origin/main)")
    parser.add_argument("--head", default="HEAD", help="head ref to diff against (default: HEAD)")
    parser.add_argument("--quiet", action="store_true", help="only print on failure")
    args = parser.parse_args()

    paths = changed_paths(args.base, args.head)
    violations = [p for p in paths if is_brand_path(p) and not is_endorsed(p)]

    if violations:
        print(
            f"FAIL: {len(violations)} changed path(s) under brand-globs but not in WHITELABEL-MODEL declared seams:",
            file=sys.stderr,
        )
        for path in violations:
            print(f"  - {path}", file=sys.stderr)
        print(
            "\nThe fork constitution (dev/unified-main/WHITELABEL-MODEL.md) declares six seams as the\n"
            "only paths fork-owned code may write to main. Brand/white-label changes anywhere else\n"
            "should be folded into one of the four patterns: prepare script (frontend), shim patch\n"
            "(backend), stage script (firmware), or profile renderer (declarative). To extend the\n"
            "endorsed-paths list, edit .github/scripts/check_whitelabel_seam.py and cite which\n"
            "pattern the new path belongs to.",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        brand_paths = [p for p in paths if is_brand_path(p)]
        print(f"OK: {len(brand_paths)} brand-path change(s); all in declared seams or endorsed fork-owned paths.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
