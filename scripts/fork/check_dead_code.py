#!/usr/bin/env python3
"""Fork wrapper around the upstream dead-code ratchet.

The upstream check_dead_code.py only reads
``${DATA_DIR_REL}/${area}.allowlist.json``. The fork's staged identity
files in app/lib/fork/identity/ are intentional fork code (wired into the
Android/iOS debug artifact by app/fork/prepare.py) but are flagged as new
unreachable code by the ratchet. Fork-owned entries belong in
``.github/scripts/dead_code/fork.${area}.allowlist.json`` so this fork's
divergence stays outside the upstream allowlist file (no merge conflict on
sync). This wrapper composes both files at scan time.

All ratchet behaviour is otherwise identical: this file just adds a
``load_allowlist`` extension that unions the upstream file's entries with
the fork file's entries.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

# Reuse every symbol from the upstream check_dead_code by importing it as a
# module. This wrapper only overrides load_allowlist / allowlist_path and
# delegates everything else.
import importlib.util

UPSTREAM_CHECK = Path(__file__).resolve().parent.parent.parent / ".github/scripts/check_dead_code.py"
_spec = importlib.util.spec_from_file_location("_upstream_check_dead_code", UPSTREAM_CHECK)
_upstream = importlib.util.module_from_spec(_spec)
# Register in sys.modules before exec_module so dataclass can resolve
# forward references (e.g. tuple[str, ...] under `from __future__ import
# annotations`) against the module's globals.
sys.modules["_upstream_check_dead_code"] = _upstream
_spec.loader.exec_module(_upstream)

# Pull every public symbol we need from the upstream module.
_ALLOWLIST_PATH = _upstream.allowlist_path
_BASELINE_PATH = _upstream.baseline_path
_LOAD_BASELINE = _upstream.load_baseline
_CHECK_AREA = _upstream.check_area
_UPDATE_BASELINE_AREA = _upstream.update_baseline_area
_SCAN_AREA = _upstream.scan_area
_AREAS = _upstream.AREAS
_DATA_DIR_REL = _upstream.DATA_DIR_REL
_DEFAULT_ROOT = _upstream.DEFAULT_ROOT
_FIX_HINT = _upstream.FIX_HINT


def _fork_allowlist_path(root: Path, area: str) -> Path:
    """Path to the fork-owned extension allowlist file, if any."""
    return root / _DATA_DIR_REL / f"fork.{area}.allowlist.json"


def _load_one_allowlist(
    path: Path, allowed: set[str], errors: list[str]
) -> None:
    """Read one allowlist JSON file, mutating ``allowed`` and ``errors``."""
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{path} is not valid JSON: {exc}")
        return
    entries = data.get("entries")
    if not isinstance(entries, list):
        errors.append(f"{path} must be an object with an 'entries' list")
        return
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"{path}: entries[{index}] must be an object with 'path' and 'reason'")
            continue
        allow_path = entry.get("path")
        reason = entry.get("reason")
        if not allow_path or not isinstance(allow_path, str):
            errors.append(f"{path}: entries[{index}] is missing a non-empty 'path'")
        if not reason or not isinstance(reason, str):
            errors.append(f"{path}: entry '{allow_path}' is missing a non-empty 'reason'; "
                          "state why the file is intentionally kept")
        if isinstance(allow_path, str) and allow_path in allowed:
            errors.append(f"{path}: duplicate entry '{allow_path}'")
        if isinstance(allow_path, str):
            allowed.add(allow_path)


def load_allowlist(root: Path, area: str) -> tuple[set[str], list[str]]:
    """Union of upstream and fork-owned allowlist entries for ``area``.

    Mirrors the upstream loader's return contract: a set of allowed paths
    and a list of schema errors. Fork-owned entries are validated against
    the same schema (path + non-empty reason) so a malformed fork entry
    fails the same way an upstream entry would.
    """
    allowed: set[str] = set()
    errors: list[str] = []
    _load_one_allowlist(_ALLOWLIST_PATH(root, area), allowed, errors)
    _load_one_allowlist(_fork_allowlist_path(root, area), allowed, errors)
    return allowed, errors


def _hint(area: str) -> str:
    """Replacement fix-hint that mentions both allowlist paths."""
    return (f"delete it, or add it to {_DATA_DIR_REL}/{area}.allowlist.json (upstream) "
            f"or {_DATA_DIR_REL}/fork.{area}.allowlist.json (fork-owned extension) with a reason")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--area", choices=_AREAS, help="scan one area instead of all four")
    parser.add_argument("--update-baseline", action="store_true",
                        help="rewrite the baseline floor(s) from the current state; "
                             "allowlist files are validated but never modified")
    parser.add_argument("--root", type=Path, default=_DEFAULT_ROOT,
                        help="repository root to scan (default: this checkout; for hermetic tests)")
    args = parser.parse_args()

    root = args.root.resolve()
    areas = (args.area,) if args.area else _AREAS
    exit_code = 0
    for area in areas:
        # Reuse the upstream scanner / checker with the overridden load_allowlist.
        # check_dead_code.check_area reads load_allowlist from the module
        # globals at call time, so monkey-patch it on _upstream for the
        # duration of this loop iteration.
        original_loader = _upstream.load_allowlist
        _upstream.load_allowlist = load_allowlist
        # The fix hint printed by check_area uses the upstream's _ALLOWLIST_PATH
        # which only points at the upstream file. Patch its formatted-string
        # output to also mention the fork path. The simplest way is to
        # temporarily rewrite the upstream module's _DAT directory name.
        original_hint = _upstream.FIX_HINT
        _upstream.FIX_HINT = _hint(area)
        try:
            if args.update_baseline:
                failures, warnings, scan = _UPDATE_BASELINE_AREA(root, area)
            else:
                failures, warnings, scan = _CHECK_AREA(root, area)
        finally:
            _upstream.load_allowlist = original_loader
            _upstream.FIX_HINT = original_hint
        for warning in warnings:
            print(f"dead-code[{area}]: note: {warning}")
        for note in scan.notes:
            print(f"dead-code[{area}]: {note}")
        if failures:
            exit_code = 1
            print(f"dead-code[{area}]: FAILED, {len(failures)} problem(s):")
            for failure in failures:
                print(f"  - {failure}")
        elif not args.update_baseline:
            fork_extra = 0
            fork_path = _fork_allowlist_path(root, area)
            if fork_path.is_file():
                try:
                    fork_extra = len(json.loads(fork_path.read_text()).get("entries", []))
                except (json.JSONDecodeError, OSError):
                    pass
            print(
                f"dead-code[{area}]: ok ({len(scan.dead)} unreachable file(s) at the baseline/allowlist floor"
                f"; {fork_extra} fork-owned allowlist entry(ies) loaded from fork.{area}.allowlist.json)"
            )
    if exit_code:
        print("dead-code check failed; new dead code must be deleted or explicitly allowlisted "
              "with a reason (see .github/scripts/dead_code/)", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())