#!/usr/bin/env python3
"""Bring the repository settings in line with config/repo-state.fork.json.

`check_repo_state.py` verifies the parts of the policy that a `GITHUB_TOKEN` can
read: which workflows exist and whether each is enabled. The other two parts --
the ruleset that makes the required checks binding, and the deployment
environment protection -- need an administration credential, so no CI job can
assert them. They are this command's job, run by an operator.

  --dry-run  (default) print every change that would be made
  --apply    make them
  --verify   read the repository back and report differences

`--apply` refuses to arm the ruleset while any required check is not currently
green on `main`. Enabling it first would block every merge on a check that was
already failing, and the usual reaction to that is to bypass the ruleset.

Exit codes: 0 clean, 1 differences found, 2 could not evaluate.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

DEFAULT_POLICY = Path('config/repo-state.fork.json')
DEFAULT_REPOSITORY = 'summersmile1984/omi'
RULESET_NAME = 'fork-main-gate'
# Repository-admin role id in GitHub's ruleset API; the break-glass hatch, so a
# broken gate is never a reason to be stuck.
ADMIN_ROLE_ID = 5


class Failure(Exception):
    """The repository could not be read or written; report exit 2."""


def api(repository: str, path: str, *, method: str = 'GET', body: dict | None = None) -> dict | list | None:
    command = ['gh', 'api', '--method', method, f'repos/{repository}/{path}']
    payload = None
    if body is not None:
        command += ['--input', '-']
        payload = json.dumps(body)
    result = subprocess.run(command, input=payload, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Failure(result.stderr.strip() or f'{method} {path} failed')
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise Failure(f'{method} {path} returned non-JSON output ({error})')


def user_id(login: str) -> int:
    result = subprocess.run(['gh', 'api', f'users/{login}'], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Failure(result.stderr.strip() or f'cannot resolve user {login}')
    try:
        return json.loads(result.stdout)['id']
    except (ValueError, KeyError) as error:
        raise Failure(f'cannot read the id of user {login} ({error})')


def ruleset_payload(policy: dict) -> dict:
    return {
        'name': RULESET_NAME,
        'target': 'branch',
        'enforcement': 'active',
        'conditions': {'ref_name': {'include': ['refs/heads/main'], 'exclude': []}},
        'rules': [
            {
                'type': 'required_status_checks',
                'parameters': {
                    # Not "strict": requiring branches to be up to date with
                    # main would serialize every merge behind a full run.
                    'strict_required_status_checks_policy': False,
                    'required_status_checks': [
                        {'context': name} for name in policy['required_checks']
                    ],
                },
            },
            {'type': 'pull_request', 'parameters': {
                'required_approving_review_count': 0,
                'dismiss_stale_reviews_on_push': False,
                'require_code_owner_review': False,
                'require_last_push_approval': False,
                'required_review_thread_resolution': False,
            }},
            {'type': 'deletion'},
            {'type': 'non_fast_forward'},
        ],
        'bypass_actors': [
            {'actor_id': ADMIN_ROLE_ID, 'actor_type': 'RepositoryRole', 'bypass_mode': 'always'}
        ],
    }


def live_ruleset(repository: str) -> dict | None:
    listing = api(repository, 'rulesets')
    for entry in listing or []:
        if entry.get('name') == RULESET_NAME:
            return api(repository, f"rulesets/{entry['id']}")
    return None


def live_environments(repository: str) -> dict[str, dict]:
    listing = api(repository, 'environments?per_page=100') or {}
    found = {}
    for entry in listing.get('environments', []):
        name = entry.get('name')
        if name:
            found[name] = api(repository, f'environments/{name}')
    return found


def live_environment_branches(repository: str, name: str) -> list[str]:
    listing = api(repository, f'environments/{name}/deployment-branch-policies') or {}
    return sorted(entry.get('name') for entry in listing.get('branch_policies', []) if entry.get('name'))


def verify_state(
    policy: dict,
    ruleset: dict | None,
    environments: dict[str, dict],
    branches: dict[str, list[str]],
) -> list[str]:
    """Compare an observed repository state with the policy.

    Split from the API reads so the comparison is testable without a repository;
    `verify_report` gathers the state and calls this.
    """
    errors: list[str] = []
    if ruleset is None:
        errors.append(f'ruleset {RULESET_NAME!r} is missing from the repository')
    else:
        required = set(policy['required_checks'])
        present = set()
        for rule in ruleset.get('rules', []):
            if rule.get('type') == 'required_status_checks':
                present = {
                    entry.get('context')
                    for entry in (rule.get('parameters') or {}).get('required_status_checks', [])
                }
        for missing in sorted(required - present):
            errors.append(f'ruleset {RULESET_NAME!r} does not require {missing!r}')
        for extra in sorted(present - required):
            errors.append(f'ruleset {RULESET_NAME!r} requires {extra!r}, which the policy does not declare')
        if ruleset.get('enforcement') != 'active':
            errors.append(f"ruleset {RULESET_NAME!r} enforcement is {ruleset.get('enforcement')!r}, not 'active'")
        if not ruleset.get('bypass_actors'):
            errors.append(f'ruleset {RULESET_NAME!r} has no break-glass bypass actor')

    for entry in policy['environments']:
        name = entry['name']
        live = environments.get(name)
        if live is None:
            errors.append(f'environment {name!r} is missing from the repository')
            continue
        allowed = branches.get(name, [])
        if allowed != sorted(entry['branches']):
            errors.append(f"environment {name!r} allows {allowed}, policy declares {sorted(entry['branches'])}")
        reviewers = live.get('reviewers') or []
        if bool(reviewers) != bool(entry['reviewers']):
            errors.append(
                f"environment {name!r} has {len(reviewers)} required reviewer(s), "
                f"policy declares {len(entry['reviewers'])}"
            )
    return errors


def verify_report(policy: dict, repository: str) -> list[str]:
    ruleset = live_ruleset(repository)
    environments = live_environments(repository)
    branches = {name: live_environment_branches(repository, name) for name in environments}
    return verify_state(policy, ruleset, environments, branches)


def plan(policy: dict, registered: dict[str, str]) -> list[str]:
    steps: list[str] = []
    for entry in policy['workflows']['keep']:
        if registered.get(entry['file']) != 'active':
            steps.append(f'enable workflow {entry["file"]}')
    for entry in policy['workflows']['disable']:
        if registered.get(entry['file']) != 'disabled_manually':
            steps.append(f'disable workflow {entry["file"]}')
    steps.append(f'create or update ruleset {RULESET_NAME!r} requiring: {", ".join(policy["required_checks"])}')
    for entry in policy['environments']:
        steps.append(
            f"set environment {entry['name']!r} to branches {entry['branches']} "
            f"with {len(entry['reviewers'])} required reviewer(s)"
        )
    return steps


def registered_workflows(repository: str) -> dict[str, str]:
    payload = api(repository, 'actions/workflows?per_page=100') or {}
    found = {}
    for entry in payload.get('workflows', []):
        path = entry.get('path') or ''
        if path.startswith('.github/workflows/'):
            found[Path(path).name] = entry.get('state')
    return found


def armable(policy: dict, repository: str) -> list[str]:
    """Required jobs whose latest run on `main` did not pass.

    Enabling the ruleset while one of these is red would block every merge on a
    check that was already failing, and the usual reaction to that is to bypass
    the ruleset. `skipped` is allowed: the upstream jobs are path-scoped and are
    routinely skipped on a push that does not touch their paths.
    """
    blocking: list[str] = []
    for entry in policy['workflows']['keep']:
        required = entry.get('required_jobs') or []
        if not required:
            continue
        payload = api(repository, f"actions/workflows/{entry['file']}/runs?branch=main&per_page=1") or {}
        runs = payload.get('workflow_runs') or []
        if not runs:
            blocking.extend(f'{name} (no run recorded on main)' for name in required)
            continue
        jobs = api(repository, f"actions/runs/{runs[0]['id']}/jobs?per_page=100") or {}
        conclusions = {job.get('name'): job.get('conclusion') for job in jobs.get('jobs', [])}
        for name in required:
            conclusion = conclusions.get(name)
            if conclusion not in ('success', 'skipped'):
                blocking.append(f'{name} ({conclusion or "not reported"})')
    return blocking


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--policy', type=Path, default=DEFAULT_POLICY)
    parser.add_argument('--repository', default=DEFAULT_REPOSITORY)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply', action='store_true', help='make the changes (default is a dry run)')
    action.add_argument('--verify', action='store_true', help='read the repository back and compare')
    args = parser.parse_args()

    try:
        policy = json.loads(args.policy.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        print(f'ERROR: cannot read {args.policy}: {error}', file=sys.stderr)
        return 2

    if args.verify:
        try:
            errors = verify_report(policy, args.repository)
        except Failure as error:
            print(f'ERROR: {error}', file=sys.stderr)
            return 2
        if errors:
            print(f'FAIL: {args.repository} does not match {args.policy}\n')
            for error in errors:
                print(f'  {error}')
            return 1
        print(f'OK: {args.repository} matches {args.policy}.')
        return 0

    try:
        registered = registered_workflows(args.repository)
    except Failure as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 2

    steps = plan(policy, registered)
    if not args.apply:
        print(f'DRY RUN against {args.repository}; {len(steps)} change(s):')
        for step in steps:
            print(f'  {step}')
        print('\nRe-run with --apply to make them.')
        return 0

    try:
        blocking = armable(policy, args.repository)
    except Failure as error:
        print(f'ERROR: cannot confirm the required checks are green: {error}', file=sys.stderr)
        return 2
    if blocking:
        print('FAIL: refusing to arm the ruleset while these required checks are failing on main:', file=sys.stderr)
        for name in blocking:
            print(f'  {name}', file=sys.stderr)
        return 1

    try:
        for entry in policy['workflows']['keep']:
            if registered.get(entry['file']) != 'active':
                api(args.repository, f"actions/workflows/{entry['file']}/enable", method='PUT')
                print(f"enabled {entry['file']}")
        for entry in policy['workflows']['disable']:
            if registered.get(entry['file']) != 'disabled_manually':
                api(args.repository, f"actions/workflows/{entry['file']}/disable", method='PUT')
                print(f"disabled {entry['file']}")
        payload = ruleset_payload(policy)
        existing = live_ruleset(args.repository)
        if existing is None:
            api(args.repository, 'rulesets', method='POST', body=payload)
            print(f'created ruleset {RULESET_NAME}')
        else:
            api(args.repository, f"rulesets/{existing['id']}", method='PUT', body=payload)
            print(f'updated ruleset {RULESET_NAME}')
        for entry in policy['environments']:
            body = {
                'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True}
            }
            if entry['reviewers']:
                body['reviewers'] = [
                    {'type': 'User', 'id': user_id(login)}
                    for login in entry['reviewers']
                ]
            api(args.repository, f"environments/{entry['name']}", method='PUT', body=body)
            for branch in entry['branches']:
                existing_branches = live_environment_branches(args.repository, entry['name'])
                if branch not in existing_branches:
                    api(
                        args.repository,
                        f"environments/{entry['name']}/deployment-branch-policies",
                        method='POST',
                        body={'name': branch, 'type': 'branch'},
                    )
            print(f"configured environment {entry['name']}")
    except Failure as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 2

    try:
        errors = verify_report(policy, args.repository)
    except Failure as error:
        print(f'ERROR: applied, but the read-back failed: {error}', file=sys.stderr)
        return 2
    if errors:
        print('FAIL: applied, but the repository still differs:')
        for error in errors:
            print(f'  {error}')
        return 1
    print(f'OK: {args.repository} now matches {args.policy}.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
