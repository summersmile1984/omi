#!/usr/bin/env python3
"""Verify the repository settings the fork's CI depends on.

Three things this fork relies on live only in GitHub's settings, not in the
tree: which upstream workflows are enabled, which checks gate `main`, and how
the four deployment environments are protected. The first one drifts by itself
-- every weekly sync can add upstream workflows that land enabled -- and the
only thing that has ever corrected it is somebody remembering to run
`gh workflow disable`. This turns that memory into a declaration
(`config/repo-state.fork.json`) plus a check.

Two modes, chosen by where it runs rather than by a flag the caller can forget:

  static  no network. The policy must agree with the tree: every workflow file
          classified exactly once, every required job name a literal job name in
          its own workflow, every quarantine entry tracked, the deploy brand
          consistent across every deploy path.
  live    `GITHUB_ACTIONS=true` or `--live`. Additionally compares the policy
          with the registered workflows and their enabled/disabled state.

The ruleset and environment assertions need an administration credential that
`GITHUB_TOKEN` does not carry, so they live in `apply_repo_state.py --verify`,
which an operator runs. Pretending CI checks them would be worse than saying so.

Exit codes: 0 clean, 1 violations found, 2 could not evaluate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

DEFAULT_POLICY = Path('config/repo-state.fork.json')
DEFAULT_REPOSITORY = 'summersmile1984/omi'
WORKFLOW_GLOBS = ('*.yml', '*.yaml')
# The paths that decide which brand is deployed. `fork-upstream-sync.yml` is
# excluded on purpose: it applies the regression-only `omi-upstream` brand.
DEPLOY_BRAND_PATHS = (
    '.github/workflows/fork-cd-cloudflare.yml',
    '.github/workflows/fork-cd-server.yml',
    '.github/workflows/fork-release-prepare.yml',
    'scripts/fork/release_ci.py',
)
REQUIRED_ENVIRONMENTS = ('cloudflare-beta', 'cloudflare-production', 'server-beta', 'server-production')
ISSUE_URL = re.compile(r'^https://github\.com/[^/]+/[^/]+/issues/\d+$')


class Failure(Exception):
    """Evaluation could not complete; the caller reports exit 2."""


def workflow_check_names(path: Path) -> set[str]:
    """The check-run names a workflow's jobs produce.

    GitHub reports a job without an explicit `name:` under its id, so both forms
    resolve to a check name here.
    """
    try:
        document = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError) as error:
        raise Failure(f'{path}: cannot be parsed ({error})')
    jobs = (document or {}).get('jobs') or {}
    if not isinstance(jobs, dict):
        raise Failure(f'{path}: jobs must be a mapping')
    names = set()
    for identifier, job in jobs.items():
        name = (job or {}).get('name') if isinstance(job, dict) else None
        names.add(str(name) if name else str(identifier))
    return names


def workflow_files(directory: Path) -> list[str]:
    found = set()
    for pattern in WORKFLOW_GLOBS:
        for path in directory.glob(pattern):
            if path.is_file():
                found.add(path.name)
    return sorted(found)


def brand_ids(root: Path) -> dict[str, Path]:
    found = {}
    for manifest in sorted((root / 'brand').glob('*/manifest.yaml')):
        try:
            document = yaml.safe_load(manifest.read_text(encoding='utf-8')) or {}
        except (OSError, yaml.YAMLError) as error:
            raise Failure(f'{manifest}: cannot be parsed ({error})')
        identifier = ((document.get('brand') or {}).get('id'))
        if not isinstance(identifier, str) or not identifier:
            raise Failure(f'{manifest}: brand.id is missing')
        found[identifier] = manifest
    return found


def static_report(policy: dict, root: Path) -> list[str]:
    errors: list[str] = []
    if policy.get('schema_version') != 1:
        errors.append('schema_version must be 1')

    workflows = policy.get('workflows')
    if not isinstance(workflows, dict):
        return errors + ['workflows must be an object']
    keep = workflows.get('keep')
    disable = workflows.get('disable')
    unregistered = workflows.get('unregistered')
    for name, section in (('keep', keep), ('disable', disable), ('unregistered', unregistered)):
        if not isinstance(section, list):
            errors.append(f'workflows.{name} must be a list')
    if errors:
        return errors

    listed: dict[str, str] = {}
    for section, entries in (('keep', keep), ('disable', disable), ('unregistered', unregistered)):
        for entry in entries:
            filename = (entry or {}).get('file')
            if not isinstance(filename, str) or not filename:
                errors.append(f'workflows.{section}: every entry needs a file')
                continue
            if filename in listed:
                errors.append(f'{filename}: classified twice ({listed[filename]} and {section})')
                continue
            listed[filename] = section
            if not (root / '.github/workflows' / filename).is_file():
                errors.append(f'{filename}: listed in {section} but not present in .github/workflows')
            if not (entry.get('reason') or '').strip():
                errors.append(f'{filename}: every entry needs a reason')

    for present in workflow_files(root / '.github/workflows'):
        if present not in listed:
            errors.append(f'{present}: present in the tree but not classified in the policy')

    required_names: dict[str, str] = {}
    for entry in keep:
        filename = entry.get('file')
        if not isinstance(filename, str):
            continue
        workflow = root / '.github/workflows' / filename
        enforcement = entry.get('enforcement')
        jobs = entry.get('required_jobs') or []
        if enforcement not in ('required', 'advisory'):
            errors.append(f'{filename}: enforcement must be required or advisory')
            continue
        if enforcement == 'required' and not jobs:
            errors.append(f'{filename}: required enforcement needs at least one required job')
        if enforcement == 'advisory' and jobs:
            errors.append(f'{filename}: advisory enforcement cannot declare required jobs')
        if not jobs or not workflow.is_file():
            continue
        names = workflow_check_names(workflow)
        for job in jobs:
            if not isinstance(job, str) or not job:
                errors.append(f'{filename}: required job names must be non-empty strings')
                continue
            # A required check is matched by name only, so an expression can
            # never be one: GitHub would look for a literal "Job (${{ ... }})".
            if '${{' in job:
                errors.append(f'{filename}: required job {job!r} contains an expression; required checks must be literal names')
                continue
            if job not in names:
                errors.append(f'{filename}: required job {job!r} is not a job in that workflow')
                continue
            if job in required_names:
                # GitHub matches required checks by name across the whole
                # repository, so a duplicate makes the requirement ambiguous.
                errors.append(f'required check {job!r} is declared by both {required_names[job]} and {filename}')
                continue
            required_names[job] = filename

    quarantine = policy.get('quarantine')
    if not isinstance(quarantine, list):
        errors.append('quarantine must be a list')
        quarantine = []
    for entry in quarantine:
        filename = (entry or {}).get('file')
        if filename not in listed or listed.get(filename) != 'keep':
            errors.append(f'quarantine: {filename} must be a kept workflow')
            continue
        kept = next((item for item in keep if item.get('file') == filename), {})
        if kept.get('required_jobs'):
            errors.append(f'{filename}: a quarantined workflow cannot contribute required checks')
        tracking = entry.get('tracking')
        if not isinstance(tracking, str) or not tracking:
            errors.append(f'{filename}: quarantine needs a tracking pointer')
        elif ISSUE_URL.match(tracking):
            pass
        elif not (root / tracking).is_file():
            errors.append(f'{filename}: quarantine tracking {tracking!r} is neither an issue URL nor an existing repository file')
        if not (entry.get('reason') or '').strip():
            errors.append(f'{filename}: quarantine needs a reason')

    declared = policy.get('required_checks')
    if not isinstance(declared, list):
        errors.append('required_checks must be a list')
    elif sorted(declared) != sorted(required_names):
        errors.append('required_checks must be exactly the required jobs declared under workflows.keep')

    brands = brand_ids(root)
    deploy_brand = policy.get('deploy_brand')
    if deploy_brand not in brands:
        errors.append(f'deploy_brand {deploy_brand!r} is not a brand manifest id ({sorted(brands)})')
    if deploy_brand == 'omi-upstream':
        errors.append('omi-upstream is the regression brand and must never be the deploy brand')
    # Static tripwire, not behavioral coverage: it fails when a deploy path names
    # a different brand, which is how a lock name and an artifact name drift
    # apart. `release_ci.py`'s own tests cover the admission behaviour itself.
    for relative in DEPLOY_BRAND_PATHS:
        path = root / relative
        if not path.is_file():
            errors.append(f'{relative}: deploy brand path is missing')
            continue
        text = path.read_text(encoding='utf-8')
        if not isinstance(deploy_brand, str) or deploy_brand not in text:
            errors.append(f'{relative}: does not name the deploy brand {deploy_brand!r}')
        for other in sorted(set(brands) - {deploy_brand}):
            if other in text:
                errors.append(f'{relative}: names {other!r}, which is not the deploy brand')

    # A dispatch input is a value somebody can type wrong, and a wrong value only
    # fails at admission time. The deploy brand is a property of this repository,
    # so it is declared here and read from the same literals in every path.
    prepare = root / '.github/workflows/fork-release-prepare.yml'
    if prepare.is_file():
        try:
            document = yaml.safe_load(prepare.read_text(encoding='utf-8')) or {}
        except yaml.YAMLError as error:
            errors.append(f'fork-release-prepare.yml: cannot be parsed ({error})')
            document = {}
        # `on:` is YAML 1.1 boolean true once parsed, however it is spelled.
        triggers = document.get('on', document.get(True)) or {}
        inputs = ((triggers or {}).get('workflow_dispatch') or {}).get('inputs') or {}
        if 'brand' in inputs:
            errors.append(
                'fork-release-prepare.yml: the brand dispatch input must be removed; '
                'the deploy brand is declared by this policy'
            )

    environments = policy.get('environments')
    if not isinstance(environments, list):
        errors.append('environments must be a list')
    else:
        names = [entry.get('name') for entry in environments if isinstance(entry, dict)]
        for required in REQUIRED_ENVIRONMENTS:
            if required not in names:
                errors.append(f'environments: {required} is not declared')
        for entry in environments:
            if not isinstance(entry, dict):
                errors.append('environments: every entry must be an object')
                continue
            if entry.get('branches') != ['main']:
                errors.append(f"environments.{entry.get('name')}: deployment branch policy must be exactly ['main']")
            reviewers = entry.get('reviewers')
            if not isinstance(reviewers, list):
                errors.append(f"environments.{entry.get('name')}: reviewers must be a list (empty means no approval)")
    return errors


def github(path: str, repository: str) -> dict:
    result = subprocess.run(
        ['gh', 'api', f'repos/{repository}/{path}'],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or f'gh api {path} failed'
        # `gh` without a token is the failure this check is most likely to hit in
        # CI, and its own message does not say which end has to change.
        if 'GH_TOKEN' in message:
            message = (
                f'{message}\n       The live lane needs a token in GH_TOKEN; '
                'fork-checks.yml passes github.token, whose permissions block makes it read-only.'
            )
        raise Failure(message)
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise Failure(f'gh api {path} returned non-JSON output ({error})')


def live_report(policy: dict, root: Path, repository: str) -> list[str]:
    errors: list[str] = []
    workflows = policy['workflows']
    payload = github('actions/workflows?per_page=100', repository)
    registered = {}
    for item in payload.get('workflows', []):
        path = item.get('path') or ''
        if path.startswith('.github/workflows/'):
            registered[Path(path).name] = item.get('state')

    expected_registered = {
        entry['file'] for entry in workflows['keep'] + workflows['disable']
    }
    for filename in sorted(set(registered) - expected_registered):
        errors.append(f'{filename}: registered on GitHub but not declared as keep or disable')
    for filename in sorted(expected_registered - set(registered)):
        errors.append(f'{filename}: declared as keep or disable but not registered on GitHub')

    for entry in workflows['keep']:
        state = registered.get(entry['file'])
        if state is not None and state != 'active':
            errors.append(f"{entry['file']}: declared keep but GitHub reports state={state!r}")
    for entry in workflows['disable']:
        state = registered.get(entry['file'])
        if state is not None and state != 'disabled_manually':
            errors.append(f"{entry['file']}: declared disable but GitHub reports state={state!r}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--policy', type=Path, default=DEFAULT_POLICY)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY') or DEFAULT_REPOSITORY)
    parser.add_argument('--live', action='store_true', help='also compare with the registered GitHub state')
    parser.add_argument('--no-live', action='store_true', help='static checks only, even in GitHub Actions')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    root = args.root.resolve()
    policy_path = args.policy if args.policy.is_absolute() else root / args.policy
    try:
        policy = json.loads(policy_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        print(f'ERROR: cannot read {policy_path}: {error}', file=sys.stderr)
        return 2

    try:
        errors = static_report(policy, root)
    except Failure as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 2

    live = args.live or (os.environ.get('GITHUB_ACTIONS') == 'true' and not args.no_live)
    if live:
        try:
            errors.extend(live_report(policy, root, args.repository))
        except Failure as error:
            # Fail closed: a run that cannot compare the declaration with the
            # repository must not report the declaration as satisfied.
            print(f'ERROR: live repository state is unavailable: {error}', file=sys.stderr)
            return 2

    if args.json:
        print(json.dumps({'ok': not errors, 'mode': 'live' if live else 'static', 'errors': errors}, indent=2))
    elif errors:
        print('FAIL: repository state does not match config/repo-state.fork.json\n')
        for error in errors:
            print(f'  {error}')
    else:
        mode = 'live' if live else 'static'
        print(f'OK: repository state policy satisfied ({mode} mode, {len(policy["required_checks"])} required checks).')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
