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


def verify_ci(run_id, sha, api=github):
    run = api(f'actions/runs/{int(run_id)}')
    successful_run(run, CI_PATH, sha)
    jobs = api(f'actions/runs/{int(run_id)}/attempts/{run["run_attempt"]}/jobs?per_page=100')['jobs']
    names = {'Fork gate (Server OS + Cloudflare)', 'Fork macOS native contracts'}
    if {job['name'] for job in jobs} != names or any(job['conclusion'] != 'success' for job in jobs):
        raise ValueError('both complete CI jobs must pass; skipped and partial runs cannot authorize delivery')
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


def verify_delivery(directory, sha, stage, target, api=github):
    directory = Path(directory).resolve()
    receipt_path = directory / 'delivery.json'
    if receipt_path.is_symlink() or receipt_path.stat().st_size > 1024 * 1024:
        raise ValueError('delivery receipt must be a bounded ordinary file')
    receipt = json.loads(receipt_path.read_text())
    if receipt.get('commit') != sha or receipt.get('stage') != stage or receipt.get('brand') != 'eddy':
        raise ValueError('delivery source, target stage or brand differs from the selected run')
    verify_ci(receipt['ci_run_id'], sha, api)
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
