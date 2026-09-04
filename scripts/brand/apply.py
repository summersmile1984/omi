#!/usr/bin/env python3
"""Render brand outputs, or compare their bytes without writing (--check-clean).

--json reports supported/rendered/skipped/partial categories and output paths.
--release rejects incomplete categories before any writes. A partial generator
(such as today's Flutter title-only generator) cannot certify an installation.
Use --manifest PATH for an explicit private brand overlay or a temporary fixture.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generators import mobile as _mobile  # noqa: E402
from manifest import ManifestError, load_manifest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
CATEGORIES = ("flutter", "desktop", "windows", "backend", "firmware", "web", "docs", "ci")
GENERATORS: dict[str, Callable[[dict], dict[str, str]]] = {"flutter": _mobile.render}
# A category joins this set only when its platform identity contract is complete.
# Flutter currently generates a title, not native app identity (audit E1).
COMPLETE_CATEGORIES: frozenset[str] = frozenset()


class ApplyError(RuntimeError):
    pass


def render(manifest: dict, only: list[str] | None = None) -> tuple[dict[str, str], dict]:
    categories = list(dict.fromkeys(only or CATEGORIES))
    if set(categories) - set(CATEGORIES):
        raise ApplyError(f"unknown categories: {sorted(set(categories) - set(CATEGORIES))}")
    outputs: dict[str, str] = {}
    rendered = []
    for category in categories:
        if category not in GENERATORS:
            continue
        files = GENERATORS[category](manifest)
        if not files:
            raise ApplyError(f"{category} generator produced no files")
        for relative, content in files.items():
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or relative in outputs:
                raise ApplyError(f"unsafe or duplicate generated path: {relative}")
            outputs[relative] = content
        rendered.append(category)
    report = {
        "brand": manifest["brand"]["id"],
        "supported": sorted(GENERATORS),
        "requested": categories,
        "rendered": rendered,
        "skipped": [c for c in categories if c not in GENERATORS],
        "partial": [c for c in rendered if c not in COMPLETE_CATEGORIES],
        "files": sorted(outputs),
    }
    report["release_ready"] = not report["skipped"] and not report["partial"]
    return outputs, report


def apply_outputs(outputs: dict[str, str], repo_root: Path, check: bool) -> list[str]:
    """Check is read-only, including absent/untracked/already-dirty outputs."""
    drift = []
    for relative, content in outputs.items():
        path = repo_root / relative
        if not path.resolve().is_relative_to(repo_root.resolve()):
            raise ApplyError(f"generated path escapes output root: {relative}")
        expected = content.encode("utf-8")
        if not path.is_file() or path.read_bytes() != expected:
            drift.append(relative)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected)
    return drift


def assess(manifest: dict, repo_root: Path, only: list[str] | None = None, release: bool = False) -> dict:
    outputs, report = render(manifest, only)
    report["drift"] = apply_outputs(outputs, repo_root, check=True)
    report["ok"] = not report["drift"] and (not release or report["release_ready"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--brand", help="brand directory id; must match --manifest when both are supplied")
    parser.add_argument("--manifest", type=Path, help="explicit YAML/JSON brand manifest outside the source tree")
    parser.add_argument("--output-root", type=Path, default=REPO_ROOT, help="isolated generated build tree")
    parser.add_argument("--only", action="append", choices=CATEGORIES)
    parser.add_argument("--check-clean", action="store_true", help="read-only byte comparison, never regenerates")
    parser.add_argument(
        "--release", action="store_true", help="require complete generators for every requested category"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.brand, REPO_ROOT, args.manifest)
        outputs, report = render(manifest, args.only)
        if args.release and not report["release_ready"]:
            report.update(ok=False, drift=[])
        else:
            report["drift"] = apply_outputs(outputs, args.output_root, args.check_clean)
            report["ok"] = not args.check_clean or not report["drift"]
    except (ManifestError, ApplyError, OSError) as error:
        if args.json:
            print(json.dumps({"ok": False, "error": str(error)}))
        else:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(
            f"{'OK' if report['ok'] else 'FAIL'}: brand {report['brand']}; rendered={report['rendered']} "
            f"skipped={report['skipped']} partial={report['partial']} release_ready={report['release_ready']}"
        )
        if args.check_clean and report["drift"]:
            print("generated bytes differ: " + ", ".join(report["drift"]))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
