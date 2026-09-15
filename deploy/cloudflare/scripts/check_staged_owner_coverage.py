#!/usr/bin/env python3
"""Fail when a source staged into the Cloudflare Workers is covered by no Cloudflare check.

`deploy/cloudflare/scripts/*_sources.py` project upstream modules into the Workers.  A
staged owner that no Cloudflare check triggers on can change under us without any lane
rebuilding the projection, and the breakage then surfaces somewhere unrelated:

  * upstream added `_budget_authority` to `backend/utils/memory/jit_trigger_snapshot.py`;
  * `jit_snapshot_sources.py` projected the reader but not its budget helper, so the
    staged kernel called an undefined name;
  * `backend/utils/memory/jit_trigger_snapshot.py` was in no Cloudflare trigger, so no
    Cloudflare lane ran, and the kernel stayed broken until an unrelated manifest refresh
    selected the lane.

Triggers are the only mechanism that makes a lane run, so the trigger list is a
correctness surface: this check keeps it complete instead of relying on a reviewer to
notice that a new `selected_nodes(...)` call needs a matching trigger.

Static checker: it asserts coverage, not that the projection behaves correctly.  The
Cloudflare lanes exercise the projection itself.
"""

from __future__ import annotations

import argparse
import ast
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / 'deploy/cloudflare/scripts'
MANIFEST = ROOT / '.github/checks-manifest.fork.yaml'
SELECTORS = {'source', 'selected_nodes'}
CHECK_PREFIX = 'fork-cloudflare-'
# This checker validates the trigger list; it can never be the lane that builds a
# projection, so it must not be able to satisfy its own coverage requirement.
COVERAGE_CHECK_ID = 'fork-cloudflare-staged-owners'


def manifest_reader():
    """Reuse the CI runner's manifest parser and trigger matcher, not a second copy."""

    sys.path.insert(0, str(ROOT / '.github/scripts'))
    return runpy.run_path(str(ROOT / '.github/scripts/run_checks.py'))


def staged_owners() -> dict[str, list[str]]:
    """Every upstream module path a Cloudflare stager reads, with the stagers that read it."""

    owners: dict[str, list[str]] = {}
    for path in sorted(SCRIPTS.glob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in SELECTORS or not node.args:
                continue
            if not isinstance(node.args[0], ast.Constant):
                continue
            value = node.args[0].value
            if isinstance(value, str) and value.endswith('.py'):
                owners.setdefault(value, []).append(path.name)
    return owners


def main() -> int:
    if sys.version_info < (3, 10):
        # Reusing the CI runner keeps one trigger matcher; that runner needs 3.10 syntax.
        print('check_staged_owner_coverage.py needs Python 3.10+; run it with backend/.venv/bin/python.')
        return 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=MANIFEST)
    args = parser.parse_args()

    run_checks = manifest_reader()
    manifest = run_checks['load_manifest'](args.manifest)
    trigger_matches = run_checks['trigger_matches']
    triggers = [
        trigger
        for check in manifest.checks
        if check.id.startswith(CHECK_PREFIX) and check.id != COVERAGE_CHECK_ID
        for trigger in check.triggers
    ]

    owners = staged_owners()
    missing = [
        (owner, stagers)
        for owner, stagers in sorted(owners.items())
        if not any(trigger_matches(trigger, owner) for trigger in triggers)
    ]
    if missing:
        print(f'{len(missing)} of {len(owners)} staged Cloudflare sources match no {CHECK_PREFIX}* trigger:')
        for owner, stagers in missing:
            print(f'  {owner}  (staged by {", ".join(stagers)})')
        print('Add each path to the triggers of the Cloudflare check that rebuilds it.')
        return 1
    print(f'All {len(owners)} staged Cloudflare sources are covered by {CHECK_PREFIX}* triggers.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
