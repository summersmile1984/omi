#!/usr/bin/env python3
"""Select the fork's complete manifest for release preparation, or its diff for CI.

In the complete lane this also writes a manifest attestation: a signed-by-run
record of which check ids the job selected, against which manifest digest. Two
jobs (portable and native) share that lane, and the release admission requires
their union to account for the whole `ci` lane -- job names alone cannot prove
it, because the same two jobs also serve the diff-scoped push and pull-request
lanes.

Execution runs one check per invocation, so a diff that breaks three unrelated
checks reports all three in one run.  The upstream runner stops at the first
failure -- `execute_checks(keep_going=...)` is wired only to `--metadata-only` --
which cost one full CI round trip per hidden failure: on 2026-09-14 the Electron
owner repair uncovered electron, then repo-state, then the Flutter anchor, one
lane each.  Fanning out keeps the upstream selector and every check's own
command, evidence and exit code intact; only the stopping rule changes, in the
fork's own wrapper rather than by patching the shared runner.

`FORK_SKIP_CHECKS` (comma-separated ids) excludes checks from one diff-scoped
run; the CI workflow uses it when no operator secret is readable and the
self-host fixture therefore cannot start.  The complete lane
(`FORK_FULL_CHECKS=true`) refuses the variable outright: its attestation must
account for every check in the manifest.
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

# Invocations that must stay single-shot: `--output`/`--list` print a selection
# document instead of running checks, and `--metadata-only` already reports every
# failure it selects (the upstream runner sets keep_going for that mode).
RESOLVE_ONLY = ('--output', '--list', '--metadata-only')


def resolves_only(arguments: list[str]) -> bool:
    return any(argument in RESOLVE_ONLY or argument.startswith('--output=') for argument in arguments)


def explicit_ids(arguments: list[str]) -> list[str]:
    """The checks the caller named, in order, however the flag was spelled."""

    identifiers: list[str] = []
    for index, argument in enumerate(arguments):
        if argument == '--check-id' and index + 1 < len(arguments):
            identifiers.append(arguments[index + 1])
        elif argument.startswith('--check-id='):
            identifiers.append(argument.split('=', 1)[1])
    return identifiers


def without_check_ids(arguments: list[str]) -> list[str]:
    """Drop `--check-id` pairs, so one fan-out invocation runs exactly one check.

    The upstream runner takes a list and runs every id it is given, so a caller's own
    `--check-id` left in the base invocation would re-run that check on every round.
    """

    result: list[str] = []
    skipping = False
    for argument in arguments:
        if skipping:
            skipping = False
            continue
        if argument == '--check-id':
            skipping = True
            continue
        if argument.startswith('--check-id='):
            continue
        result.append(argument)
    return result


def skip_check_ids() -> list[str]:
    """The checks the caller excluded from this run via `FORK_SKIP_CHECKS` (comma separated).

    The CI workflow sets it when no operator secret is readable: the self-host fixture
    refuses a native local text model at admission and the manifest forbids fake
    inference, so `fork-selfhost-product-core` is skipped instead of failing every run
    without secrets. Selection probes (`--output`/`--list`) deliberately ignore it --
    the workflow's provisioning decisions read those probes.
    """

    raw = os.environ.get('FORK_SKIP_CHECKS', '')
    return [identifier for identifier in (part.strip() for part in raw.split(',')) if identifier]


def without_skipped(check_ids: list[str], skipped: list[str]) -> tuple[list[str], list[str]]:
    """Partition a selection into (retained, excluded) against the skip list, order kept."""

    excluded = set(skipped)
    retained = [identifier for identifier in check_ids if identifier not in excluded]
    return retained, [identifier for identifier in check_ids if identifier in excluded]


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
        # The complete lane's selection is expressed as explicit ids, which is also what
        # its `--output json` probe reports. Fan-out strips them again and passes one per
        # invocation, because the upstream runner runs every id it is given.
        for identifier in selected:
            result.extend(['--check-id', identifier])
    return result, selected, options.lane, options.platform


def selection_ids(invocation: list[str]) -> list[str] | None:
    """Ask the upstream selector which checks this diff selects, instead of copying it."""

    probe = subprocess.run([*invocation, '--output', 'json'], cwd=ROOT, capture_output=True, text=True, check=False)
    if probe.returncode:
        return None
    try:
        return [check['id'] for check in json.loads(probe.stdout)['checks']]
    except (KeyError, TypeError, ValueError):
        return None


def execute_each(invocation: list[str], check_ids: list[str], *, run=subprocess.call) -> int:
    """Run every selected check and report the whole set of failures."""

    print(f'Fork manifest: running {len(check_ids)} selected check(s) to completion', flush=True)
    failures: list[str] = []
    for identifier in check_ids:
        if run([*invocation, '--check-id', identifier], cwd=ROOT):
            failures.append(identifier)
    if failures:
        print(f"Fork manifest checks failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f'Fork manifest checks passed: {len(check_ids)} check(s).')
    return 0


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
    skipped = skip_check_ids()
    if full and skipped:
        # The complete lane's attestation is proof the whole manifest ran; a skip
        # list would silently under-report it, so refuse before selecting anything.
        raise SystemExit(
            f'FORK_SKIP_CHECKS ({", ".join(skipped)}) conflicts with FORK_FULL_CHECKS=true: '
            'the complete lane must select, run and attest every check.'
        )
    invocation, selected, lane, platform = command(arguments, full)
    destination = os.environ.get('FORK_CI_ATTESTATION')
    # Only the complete lane has a known selection: the diff lane's ids are
    # decided by the upstream selector at run time, and attesting to them would
    # mean reimplementing that selector here.
    if destination and full:
        # The attestation write must never abort the lane: the workflow runs the
        # selector with `FORK_CI_ATTESTATION` set in the job env so the release
        # admission can read it back, and a write failure on a non-writable or
        # otherwise constrained path would otherwise turn the gate red for the
        # selector itself. Print the failure to stderr (callers parse stdout
        # only) and fall through to the upstream call.
        try:
            attestation(Path(destination), lane, platform, selected)
        except OSError as error:
            print(
                f'warning: could not write manifest attestation to {destination}: {error}',
                file=sys.stderr,
                flush=True,
            )
    if resolves_only(arguments):
        raise SystemExit(subprocess.call(invocation, cwd=ROOT))
    check_ids = explicit_ids(arguments) or (selected if full else selection_ids(invocation))
    if not check_ids:
        # Nothing selected, or the upstream selector could not answer: run the
        # entry point itself so its own summary and exit code stand.
        raise SystemExit(subprocess.call(invocation, cwd=ROOT))
    if skipped:
        retained, excluded = without_skipped(check_ids, skipped)
        if excluded:
            print(
                f'Fork manifest: skipping {len(excluded)} check(s): {", ".join(excluded)}',
                file=sys.stderr,
                flush=True,
            )
        check_ids = retained
    raise SystemExit(execute_each(without_check_ids(invocation), check_ids))
