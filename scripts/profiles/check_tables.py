#!/usr/bin/env python3
"""Verify the deployment-profile source against upstream and across platforms.

Three properties, in the order they matter:

1. **Upstream equivalence.** Rendering `omi_cloud` must reproduce the literals in
   upstream's `app/lib/env/environment_profile.dart`. Those values are parsed out
   of the upstream file at runtime, so upstream editing them fails this check on
   the next sync instead of the fork silently drifting.
2. **The selected target resolves.** Required capability and data-plane keys present,
   identity provider correctly derived, https enforced where the stage demands it.
3. **All five generated outputs are present and current.** `render.py --check`
   against the selected build root. Default scope is omi_cloud. The CI-wired
   test_profiles.py exercises both fork targets using temporary manifests.

Usage: scripts/profiles/check_tables.py [--target TARGET] [--manifest PATH] [--output-root DIR]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from render import ProfileError, resolve  # noqa: E402

UPSTREAM_ENUM = REPO_ROOT / "app/lib/env/environment_profile.dart"

# upstream profile name -> the resolved profile that must match it
EQUIVALENCE = {
    "production": "omi_cloud.production",
    "mobile_beta": "omi_cloud.beta",
    "local_dev": "omi_cloud.local",
    "local_prod": "local_prod",
}

FIELD_MAP = {
    "defaultApiBaseUrl": "api_base_url",
    "firebaseProjectId": "firebase_project_id",
    "authCallbackScheme": "auth_callback_scheme",
    "usesFirebaseAuthEmulator": "uses_firebase_auth_emulator",
    "allowsProductionData": "allows_production_data",
}

# The final enum entry ends with ");" rather than "),", so accept both.
ENTRY_RE = re.compile(r"\b(\w+)\(\s*\n\s*name:\s*'([^']+)',(.*?)\n\s*\)[,;]", re.S)
FIELD_RE = re.compile(r"(\w+):\s*(?:'([^']*)'|(true|false))")


def parse_upstream_enum(path: Path) -> dict[str, dict]:
    """Read the upstream enum's literals so the check tracks upstream, not a copy."""
    if not path.exists():
        raise ProfileError(f"upstream enum not found: {path}")
    text = path.read_text(encoding="utf-8")
    body = text[text.index("enum AppEnvironmentProfile {") :]
    body = body[: body.index("\n  const AppEnvironmentProfile(")]
    out: dict[str, dict] = {}
    for match in ENTRY_RE.finditer(body + ","):
        name, fields = match.group(2), match.group(3)
        row: dict[str, object] = {}
        for key, string_value, bool_value in FIELD_RE.findall(fields):
            if key in FIELD_MAP:
                row[FIELD_MAP[key]] = string_value if bool_value == "" else bool_value == "true"
        out[name] = row
    return out


def check_equivalence() -> list[str]:
    problems: list[str] = []
    upstream = parse_upstream_enum(UPSTREAM_ENUM)
    resolved = resolve("omi_cloud", "omi-upstream")["profiles"]

    missing = sorted(set(EQUIVALENCE) - set(upstream))
    if missing:
        problems.append(
            f"upstream enum no longer defines {', '.join(missing)}; upstream changed its profile set "
            f"-- update deploy/profiles/omi_cloud.yaml and this mapping together"
        )
    extra = sorted(set(upstream) - set(EQUIVALENCE))
    if extra:
        problems.append(
            f"upstream enum gained {', '.join(extra)}; add it to deploy/profiles/omi_cloud.yaml "
            f"so the equivalence probe still covers every upstream profile"
        )

    for upstream_name, resolved_name in EQUIVALENCE.items():
        if upstream_name not in upstream:
            continue
        if resolved_name not in resolved:
            problems.append(f"{resolved_name} is not produced by omi_cloud.yaml")
            continue
        for field, want in upstream[upstream_name].items():
            got = resolved[resolved_name].get(field)
            if got != want:
                problems.append(f"{upstream_name} -> {resolved_name}: {field} is {got!r}, upstream says {want!r}")
    return problems


def check_targets(brand: str | None, targets: list[str], manifest_path: Path | None = None) -> list[str]:
    problems = []
    for target in targets:
        try:
            resolve(target, brand, manifest_path)
        except ProfileError as error:
            problems.append(str(error))
    return problems


def check_generated(brand: str | None, target: str, manifest_path: Path | None, output_root: Path) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve().parent / "render.py"),
        "--target",
        target,
        "--check",
        "--output-root",
        str(output_root),
    ]
    if brand:
        command.extend(["--brand", brand])
    if manifest_path:
        command.extend(["--manifest", str(manifest_path)])
    proc = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT)
    return [proc.stdout.strip() or proc.stderr.strip()] if proc.returncode else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--brand")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--target", choices=("omi_cloud", "self_hosted", "cloudflare"), default="omi_cloud")
    parser.add_argument("--output-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args()

    sections = [
        ("upstream equivalence (omi_cloud vs app/lib/env/environment_profile.dart)", check_equivalence),
        (f"target resolution ({args.target})", lambda: check_targets(args.brand, [args.target], args.manifest)),
        (
            "generated tables current (all five outputs required)",
            lambda: check_generated(args.brand, args.target, args.manifest, args.output_root),
        ),
    ]

    failed = False
    for title, run in sections:
        try:
            problems = run()
        except ProfileError as error:
            problems = [str(error)]
        if problems:
            failed = True
            print(f"FAIL: {title}")
            for problem in problems:
                print(f"  {problem}")
        else:
            print(f"ok: {title}")

    if failed:
        print("\nSource of truth: deploy/profiles/  ·  design: dev/unified-main/02-deployment-profile.md")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
