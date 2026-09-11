#!/usr/bin/env python3
"""Select the fork's complete manifest for release preparation, or its diff for CI.

In the complete lane this also writes a manifest attestation: a signed-by-run
record of which check ids the job selected, against which manifest digest. Two
jobs (portable and native) share that lane, and the release admission requires
their union to account for the whole `ci` lane -- job names alone cannot prove
it, because the same two jobs also serve the diff-scoped push and pull-request
lanes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / '.github/checks-manifest.fork.yaml'


def command(arguments: list[str], full: bool) -> tuple[list[str], list[str], str, str]:
    # Reuse the upstream manifest parser and execution owner; no copied checks.
    sys.path.insert(0, str(ROOT / '.github/scripts'))
    from run_checks import _platform_matches, detect_platform, load_manifest

    result = [sys.executable, str(ROOT / '.github/scripts/run_checks.py'), '--manifest', str(MANIFEST), *arguments]
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--platform', default=detect_platform())
    parser.add_argument('--exclusive-platform', action='store_true')
    parser.add_argument('--lane', required=True)
    options, _ = parser.parse_known_args(arguments)
    selected: list[str] = []
    if full:
        for check in load_manifest(MANIFEST).checks:
            if options.lane in check.lanes and _platform_matches(
                check, options.platform, exclusive=options.exclusive_platform
            ):
                selected.append(check.id)
        for identifier in selected:
            result.extend(['--check-id', identifier])
    return result, selected, options.lane, options.platform


def attestation(path: Path, lane: str, platform: str, check_ids: list[str]) -> None:
    run_id = os.environ.get('GITHUB_RUN_ID')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT')
    payload = {
        'schema_version': 1,
        'lane': lane,
        'platform': platform,
        'check_ids': sorted(check_ids),
        'manifest': str(MANIFEST.relative_to(ROOT)),
        'manifest_sha256': hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        'repository': os.environ.get('GITHUB_REPOSITORY'),
        'run_id': int(run_id) if run_id and run_id.isdigit() else None,
        'run_attempt': int(attempt) if attempt and attempt.isdigit() else None,
        'sha': os.environ.get('GITHUB_SHA'),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    # stderr, never stdout: the inner runner's stdout carries `--output json`,
    # which callers parse. The CI job sets FORK_CI_ATTESTATION in the job env, so
    # a line here broke `test_release_prepare.py` in the workflow while passing
    # locally, where the variable is unset.
    print(f'manifest attestation: {len(check_ids)} check(s) -> {path}', file=sys.stderr, flush=True)


if __name__ == '__main__':
    arguments = sys.argv[1:]
    full = os.environ.get('FORK_FULL_CHECKS') == 'true'
    invocation, selected, lane, platform = command(arguments, full)
    destination = os.environ.get('FORK_CI_ATTESTATION')
    # Only the complete lane has a known selection: the diff lane's ids are
    # decided by the upstream selector at run time, and attesting to them would
    # mean reimplementing that selector here.
    if destination and full:
        attestation(Path(destination), lane, platform, selected)
    raise SystemExit(subprocess.call(invocation, cwd=ROOT))
