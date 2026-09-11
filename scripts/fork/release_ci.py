#!/usr/bin/env python3
"""Resolve release provenance from GitHub and verify downloaded delivery bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from release_archive import unpack_candidate

REPOSITORY = 'summersmile1984/omi'
CI_PATH = '.github/workflows/fork-checks.yml'
PREPARE_PATH = '.github/workflows/fork-release-prepare.yml'
RELEASE_JOBS = {
    'Freeze delivery artifacts',
    'Cloudflare artifact qualification / Resolve delivery',
    'Cloudflare artifact qualification / Execute accepted delivery',
    'Server image qualification / Resolve delivery',
    'Server image qualification / Execute accepted delivery',
    'Release ready (runtime and public ingress)',
}
# The full CI lane publishes one attestation per job (portable and native); the
# artifact name is bound to the source commit and the file name is fixed so the
# reader does not have to guess.
ATTESTATION_PREFIX = 'fork-ci-attestation-'
ATTESTATION_MANIFEST = '.github/checks-manifest.fork.yaml'
ATTESTATION_FILENAME = 'fork-ci-attestation.json'


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def github(path):
    result = subprocess.run(['gh', 'api', f'repos/{REPOSITORY}/{path}'], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def successful_run(run, path, sha=None):
    if (
        run.get('repository', {}).get('full_name') != REPOSITORY
        or run.get('head_repository', {}).get('full_name') != REPOSITORY
        or run.get('path') != path
        or run.get('event') != 'workflow_dispatch'
        or run.get('status') != 'completed'
        or run.get('conclusion') != 'success'
        or not re.fullmatch(r'[0-9a-f]{40}', run.get('head_sha', ''))
        or (sha and run['head_sha'] != sha)
    ):
        raise ValueError('release requires a successful full manual run from this fork at the exact source SHA')
    return run['head_sha']


def manifest_path(manifest=ATTESTATION_MANIFEST):
    """Resolve the manifest against the checked-out workspace, not the process cwd.

    Both real callers run from the repository root, but CD loads this script from
    `RUNNER_TEMP` and a future caller could run it from elsewhere; `verify_ci`
    must compare against the manifest of the source it is admitting, and that is
    the one in `GITHUB_WORKSPACE`.
    """
    workspace = os.environ.get('GITHUB_WORKSPACE')
    return Path(workspace) / manifest if workspace else Path(manifest)


def load_manifest(path):
    """Parse the fork manifest with the repository's own stdlib-only loader.

    PyYAML is not available where this runs first: the prepare job checks its CI
    run before it provisions the backend environment, on purpose, and that step
    only has the runner's system Python. `.github/scripts/run_checks.py` already
    owns a dependency-free parser for this exact file, so reuse it rather than
    adding a second reader or a dependency.
    """
    scripts = Path(os.environ.get('GITHUB_WORKSPACE') or Path.cwd()) / '.github' / 'scripts'
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from run_checks import load_manifest as load

    return load(path)


def expected_ci_checks(manifest=ATTESTATION_MANIFEST):
    """Every check the `ci` lane declares in the manifest at the delivered source.

    The property being certified is coverage, not which job ran what: the
    portable and native lanes each attest to the ids they selected, and together
    they must account for the whole lane.
    """
    path = manifest_path(manifest)
    if not path.is_file():
        raise ValueError(f'the fork manifest is unavailable to the CI admission: {path}')
    checks = load_manifest(path).checks
    if not checks:
        raise ValueError('the fork manifest declares no checks')
    return {str(check.id) for check in checks if 'ci' in check.lanes}


def list_attestations(run_id, sha, api=github):
    """The unexpired manifest attestations this run published for this source.

    `actions/runs/{id}/artifacts` is the only listing endpoint: the
    `/attempts/{n}/artifacts` form returns 404, which is how the first real run of
    this admission path failed. The attempt is still bound by the payload, which
    carries the run id and attempt and is checked against the selected attempt.
    """
    artifacts = api(f'actions/runs/{int(run_id)}/artifacts?per_page=100')['artifacts']
    return [
        item
        for item in artifacts
        if item.get('name', '').startswith(ATTESTATION_PREFIX)
        and item['name'].endswith(f'-{sha}')
        and not item.get('expired')
    ]


def download_attestations(run_id, attempt, sha, api=github):
    """Fetch the manifest attestations the CI run published for this source."""
    selected = list_attestations(run_id, sha, api)
    if not selected:
        raise ValueError('the CI run published no manifest attestation for this source')
    payloads = []
    for item in selected:
        with tempfile.TemporaryDirectory(prefix='fork-ci-attestation-') as directory:
            result = subprocess.run(
                ['gh', 'run', 'download', str(int(run_id)), '--repo', REPOSITORY, '--name', item['name'], '--dir', directory],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise ValueError(f"cannot download the manifest attestation {item['name']}")
            path = Path(directory) / ATTESTATION_FILENAME
            if not path.is_file():
                raise ValueError(f"the manifest attestation {item['name']} has no {ATTESTATION_FILENAME}")
            payloads.append(json.loads(path.read_text(encoding='utf-8')))
    return payloads


def verify_attestations(payloads, run_id, attempt, sha, manifest=ATTESTATION_MANIFEST):
    """Require the published attestations to account for the whole `ci` lane.

    The job-name check above proves two jobs passed; it cannot prove they ran
    the complete manifest, because the same two jobs also serve the diff-scoped
    push and pull-request lanes. This is that missing half.
    """
    digest = sha256_file(manifest_path(manifest))
    expected = expected_ci_checks(manifest)
    covered = set()
    for payload in payloads:
        if payload.get('schema_version') != 1:
            raise ValueError('manifest attestation has an unsupported schema version')
        if payload.get('lane') != 'ci':
            raise ValueError('manifest attestation was not produced by the CI lane')
        if payload.get('sha') != sha:
            raise ValueError('manifest attestation belongs to a different source')
        if str(payload.get('run_id')) != str(int(run_id)) or str(payload.get('run_attempt')) != str(int(attempt)):
            raise ValueError('manifest attestation belongs to a different run attempt')
        if payload.get('manifest_sha256') != digest:
            raise ValueError('manifest attestation was produced against a different manifest')
        identifiers = payload.get('check_ids')
        if not isinstance(identifiers, list) or not identifiers or not all(isinstance(item, str) for item in identifiers):
            raise ValueError('manifest attestation declares no check ids')
        covered.update(identifiers)
    missing = sorted(expected - covered)
    if missing:
        raise ValueError(
            'the CI run did not execute the complete manifest; missing: ' + ', '.join(missing)
        )
    return covered


def verify_ci(run_id, sha, api=github, attestations=None):
    run = api(f'actions/runs/{int(run_id)}')
    successful_run(run, CI_PATH, sha)
    jobs = api(f'actions/runs/{int(run_id)}/attempts/{run["run_attempt"]}/jobs?per_page=100')['jobs']
    names = {'Fork gate (Server OS + Cloudflare)', 'Fork macOS native contracts'}
    if {job['name'] for job in jobs} != names or any(job['conclusion'] != 'success' for job in jobs):
        raise ValueError('both complete CI jobs must pass; skipped and partial runs cannot authorize delivery')
    reader = attestations or download_attestations
    payloads = reader(int(run_id), run['run_attempt'], sha, api)
    verify_attestations(payloads, run_id, run['run_attempt'], sha)
    return {'ci_run_id': int(run_id), 'ci_run_attempt': run['run_attempt'], 'commit': sha}


def resolve_delivery(run_id, stage, api=github):
    run = api(f'actions/runs/{int(run_id)}')
    sha = successful_run(run, PREPARE_PATH)
    jobs = api(f'actions/runs/{int(run_id)}/attempts/{run["run_attempt"]}/jobs?per_page=100')['jobs']
    if {job['name'] for job in jobs} != RELEASE_JOBS or any(job['conclusion'] != 'success' for job in jobs):
        raise ValueError('release CI must qualify transported artifacts, actual runtime readiness and public ingress')
    comparison = api(f'compare/{sha}...main')
    if comparison.get('status') not in {'ahead', 'identical'}:
        raise ValueError('deployment source must be integrated into main')
    name = f'delivery-{sha}-eddy-{stage}'
    artifacts = api(f'actions/runs/{int(run_id)}/artifacts?per_page=100')['artifacts']
    matches = [item for item in artifacts if item['name'] == name and not item['expired']]
    if len(matches) != 1:
        raise ValueError('the selected run has no unique unexpired artifact for this brand and stage')
    return {'sha': sha, 'artifact': name, 'run_id': int(run_id)}


def verify_delivery(directory, sha, stage, target, api=github, attestations=None):
    directory = Path(directory).resolve()
    receipt_path = directory / 'delivery.json'
    if receipt_path.is_symlink() or receipt_path.stat().st_size > 1024 * 1024:
        raise ValueError('delivery receipt must be a bounded ordinary file')
    receipt = json.loads(receipt_path.read_text())
    if receipt.get('commit') != sha or receipt.get('stage') != stage or receipt.get('brand') != 'eddy':
        raise ValueError('delivery source, target stage or brand differs from the selected run')
    verify_ci(receipt['ci_run_id'], sha, api, attestations)
    filename = 'cloudflare.tar.gz' if target == 'cloudflare' else 'server-images.tar'
    path = directory / filename
    if path.is_symlink() or not path.is_file():
        raise ValueError('delivery archive must be an ordinary file')
    actual = sha256_file(path)
    if actual != receipt['files'][filename]:
        raise ValueError('delivery archive hash differs from its accepted receipt')
    if target == 'cloudflare':
        unpack_candidate(path, directory / 'unpacked', receipt['candidate_digest'])
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['ci', 'resolve', 'verify'])
    parser.add_argument('--run-id', type=int)
    parser.add_argument('--sha')
    parser.add_argument('--stage', choices=['beta', 'production'])
    parser.add_argument('--target', choices=['cloudflare', 'self_hosted'])
    parser.add_argument('--directory', type=Path)
    args = parser.parse_args()
    if args.operation == 'ci':
        result = verify_ci(args.run_id, args.sha)
    elif args.operation == 'resolve':
        result = resolve_delivery(args.run_id, args.stage)
    else:
        result = verify_delivery(args.directory, args.sha, args.stage, args.target)
    if args.operation == 'resolve' and os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            for key, value in result.items():
                output.write(f'{key}={value}\n')
    print(json.dumps({key: result[key] for key in ('sha', 'commit', 'ci_run_id', 'artifact') if key in result}))


if __name__ == '__main__':
    main()
