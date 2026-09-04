#!/usr/bin/env python3
"""Render every generated file for one brand from its manifest.

Idempotent: running twice produces zero diff on the second run. `--only`
restricts to one category (flutter, desktop, windows, backend, firmware,
web, docs, ci); omit it to render everything a brand needs.

B0 shipped the registry and validation path with zero generators registered;
B1 through B7 each register one category's generator (one category each; see
dev/unified-main/04-brand-layer.md §4). A category with no renderer yet is
still a manifest-validation dry run for that slice -- `apply.py --brand <any>
--check-clean` only becomes a meaningful regression guarantee for a category
once that category's generator lands, not before.

Usage:
    scripts/brand/apply.py --brand <id> [--only CATEGORY ...] [--check-clean]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema_validate import validate  # noqa: E402
from yaml_lite import load_yaml  # noqa: E402
from generators import mobile as _mobile  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "brand/_schema/manifest.schema.json"
BRAND_ROOT = REPO_ROOT / "brand"

# One entry per B1-B7 PR. A generator is `(manifest: dict, repo_root: Path) -> list[Path]`,
# returning every path it wrote so --check-clean can verify idempotency without
# re-deriving what "this category's output" means.
CATEGORIES: tuple[str, ...] = (
    "flutter",
    "desktop",
    "windows",
    "backend",
    "firmware",
    "web",
    "docs",
    "ci",
)

GENERATORS: dict[str, Callable[[dict, Path], list[Path]]] = {
    "flutter": _mobile.render,
}


class ApplyError(RuntimeError):
    pass


def load_manifest(brand_id: str) -> dict:
    manifest_path = BRAND_ROOT / brand_id / "manifest.yaml"
    if not manifest_path.exists():
        raise ApplyError(
            f"no manifest at {manifest_path.relative_to(REPO_ROOT)}. "
            f"Known brands: {sorted(p.name for p in BRAND_ROOT.iterdir() if (p / 'manifest.yaml').exists())}"
        )
    manifest = load_yaml(manifest_path)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = validate(manifest, schema)
    if errors:
        formatted = "\n".join(f"  - {e}" for e in errors)
        raise ApplyError(f"{manifest_path.relative_to(REPO_ROOT)} does not match the schema:\n{formatted}")
    return manifest


def render(manifest: dict, only: list[str] | None) -> list[Path]:
    categories = only or list(CATEGORIES)
    unknown = [c for c in categories if c not in CATEGORIES]
    if unknown:
        raise ApplyError(
            f"unknown --only categor{'y' if len(unknown) == 1 else 'ies'}: {unknown}. Choices: {list(CATEGORIES)}"
        )

    written: list[Path] = []
    skipped: list[str] = []
    for category in categories:
        generator = GENERATORS.get(category)
        if generator is None:
            skipped.append(category)
            continue
        written.extend(generator(manifest, REPO_ROOT))

    if skipped:
        print(
            f"no generator registered yet for: {', '.join(skipped)} "
            f"(dev/unified-main/04-brand-layer.md §4 -- lands in B1-B7)",
            file=sys.stderr,
        )
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--brand", required=True, help="brand id -- a directory name under brand/")
    parser.add_argument("--only", action="append", choices=CATEGORIES, help="restrict to one category; repeatable")
    parser.add_argument(
        "--check-clean",
        action="store_true",
        help="fail if rendering would change any file already on disk (CI idempotency gate)",
    )
    args = parser.parse_args()

    try:
        manifest = load_manifest(args.brand)
        import subprocess

        before = None
        if args.check_clean:
            before = subprocess.run(
                ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
            ).stdout
        written = render(manifest, args.only)
        if args.check_clean:
            after = subprocess.run(
                ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
            ).stdout
            if before != after:
                print("FAIL: apply.py --check-clean found a diff after rendering:", file=sys.stderr)
                print(after, file=sys.stderr)
                return 1
    except ApplyError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(
        f"OK: brand '{args.brand}' -- {len(written)} file(s) written"
        if written
        else f"OK: brand '{args.brand}' -- nothing to render yet"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
