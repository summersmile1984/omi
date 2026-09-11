#!/usr/bin/env python3
"""Behavioral tests for the repository state declaration and its checker.

Each test builds a throwaway tree with its own brand manifests, workflows and
policy, then runs the checker as a subprocess for the static lane and calls
`live_report` directly with a stubbed API for the live lane.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'check_repo_state.py'

PREPARE = """\
name: Fork Release CI
on:
  workflow_dispatch:
    inputs:
      ci_run_id:
        required: true
        type: string
      stage:
        required: true
        type: choice
        options: [beta, production]
concurrency:
  group: fork-prepare-${{ github.ref }}-eddy-${{ inputs.stage }}
  cancel-in-progress: false
jobs:
  prepare:
    name: Freeze delivery artifacts
    runs-on: ubuntu-latest
    steps: []
"""

FORK_CHECKS = """\
name: Fork Checks
on:
  pull_request:
  workflow_dispatch:
jobs:
  gate:
    name: Fork gate (Server OS + Cloudflare)
    runs-on: ubuntu-latest
    steps: []
  macos:
    name: Fork macOS native contracts
    runs-on: ubuntu-latest
    steps: []
"""

CD = """\
name: Fork CD {target}
on:
  workflow_dispatch:
concurrency:
  group: fork-cd-{target}-eddy-${{{{ inputs.stage }}}}
  cancel-in-progress: false
jobs:
  resolve:
    name: Resolve delivery
    runs-on: ubuntu-latest
    steps: []
"""

BRAND = """\
schema_version: 1
brand:
  id: {identifier}
  display_name: {identifier}
"""


def load_module():
    spec = importlib.util.spec_from_file_location('check_repo_state', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepoStateHarness:
    def __init__(self, root: Path) -> None:
        self.root = root
        for identifier in ('eddy', 'omi-upstream'):
            manifest = root / 'brand' / identifier / 'manifest.yaml'
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(BRAND.format(identifier=identifier), encoding='utf-8')
        workflows = root / '.github' / 'workflows'
        workflows.mkdir(parents=True, exist_ok=True)
        (workflows / 'fork-checks.yml').write_text(FORK_CHECKS, encoding='utf-8')
        (workflows / 'fork-cd-cloudflare.yml').write_text(CD.format(target='cloudflare'), encoding='utf-8')
        (workflows / 'fork-cd-server.yml').write_text(CD.format(target='server'), encoding='utf-8')
        (workflows / 'fork-release-prepare.yml').write_text(PREPARE, encoding='utf-8')
        (workflows / 'upstream.yml').write_text('name: Upstream\non:\n  workflow_dispatch:\njobs:\n  lint:\n    name: Lint\n    runs-on: ubuntu-latest\n    steps: []\n', encoding='utf-8')
        (workflows / 'never-registered.yml').write_text('name: Never\non:\n  workflow_dispatch:\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps: []\n', encoding='utf-8')
        scripts = root / 'scripts' / 'fork'
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / 'release_ci.py').write_text("BRAND = 'eddy'\n", encoding='utf-8')
        tracking = root / 'dev' / 'unified-main'
        tracking.mkdir(parents=True, exist_ok=True)
        (tracking / '05-ci-matrix.md').write_text('# CI matrix\n', encoding='utf-8')
        self.policy = {
            'schema_version': 1,
            'deploy_brand': 'eddy',
            'workflows': {
                'keep': [
                    {
                        'file': 'fork-checks.yml',
                        'enforcement': 'required',
                        'reason': 'the fork gate',
                        'required_jobs': ['Fork gate (Server OS + Cloudflare)', 'Fork macOS native contracts'],
                    },
                    {
                        'file': 'fork-release-prepare.yml',
                        'enforcement': 'advisory',
                        'reason': 'freeze lane',
                    },
                    {'file': 'fork-cd-cloudflare.yml', 'enforcement': 'advisory', 'reason': 'cloudflare CD'},
                    {'file': 'fork-cd-server.yml', 'enforcement': 'advisory', 'reason': 'server CD'},
                ],
                'disable': [{'file': 'upstream.yml', 'reason': 'upstream deploy'}],
                'unregistered': [{'file': 'never-registered.yml', 'reason': 'never registered by GitHub'}],
            },
            'quarantine': [],
            'required_checks': ['Fork gate (Server OS + Cloudflare)', 'Fork macOS native contracts'],
            'environments': [
                {'name': 'cloudflare-beta', 'branches': ['main'], 'reviewers': []},
                {'name': 'cloudflare-production', 'branches': ['main'], 'reviewers': ['summersmile1984']},
                {'name': 'server-beta', 'branches': ['main'], 'reviewers': []},
                {'name': 'server-production', 'branches': ['main'], 'reviewers': ['summersmile1984']},
            ],
        }

    def write_policy(self) -> Path:
        path = self.root / 'config' / 'repo-state.fork.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.policy, indent=2), encoding='utf-8')
        return path


class StaticReportTests(unittest.TestCase):
    def harness(self) -> RepoStateHarness:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return RepoStateHarness(Path(tmp.name))

    def check(self, harness: RepoStateHarness) -> subprocess.CompletedProcess:
        policy = harness.write_policy()
        return subprocess.run(
            [sys.executable, str(SCRIPT), '--policy', str(policy), '--root', str(harness.root), '--no-live'],
            capture_output=True,
            text=True,
        )

    def assertFails(self, harness: RepoStateHarness, fragment: str) -> None:
        result = self.check(harness)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(fragment, result.stdout + result.stderr)

    def test_a_complete_policy_passes(self):
        harness = self.harness()
        result = self.check(harness)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('OK: repository state policy satisfied', result.stdout)

    def test_every_workflow_file_must_be_classified(self):
        harness = self.harness()
        (harness.root / '.github' / 'workflows' / 'unnamed.yml').write_text('name: U\non:\n  workflow_dispatch:\n', encoding='utf-8')
        self.assertFails(harness, 'unnamed.yml: present in the tree but not classified')

    def test_a_file_cannot_be_classified_twice(self):
        harness = self.harness()
        harness.policy['workflows']['disable'].append({'file': 'fork-checks.yml', 'reason': 'duplicate'})
        self.assertFails(harness, 'fork-checks.yml: classified twice')

    def test_required_job_must_exist_in_its_workflow(self):
        harness = self.harness()
        harness.policy['workflows']['keep'][0]['required_jobs'].append('Nonexistent Job')
        harness.policy['required_checks'].append('Nonexistent Job')
        self.assertFails(harness, "'Nonexistent Job' is not a job in that workflow")

    def test_required_job_must_be_a_literal_name(self):
        harness = self.harness()
        harness.policy['workflows']['keep'][0]['required_jobs'] = ['gate (${{ matrix.os }})']
        harness.policy['required_checks'] = ['gate (${{ matrix.os }})']
        self.assertFails(harness, 'contains an expression')

    def test_required_job_names_must_not_repeat(self):
        # GitHub matches a required check by name across the whole repository,
        # so two workflows claiming the same name make the requirement
        # ambiguous about which one actually covers the branch.
        harness = self.harness()
        server = harness.root / '.github' / 'workflows' / 'fork-cd-server.yml'
        server.write_text(
            CD.format(target='server').replace(
                '  resolve:\n    name: Resolve delivery\n',
                '  resolve:\n    name: Resolve delivery\n  copy:\n    name: Fork macOS native contracts\n',
            ),
            encoding='utf-8',
        )
        harness.policy['workflows']['keep'][3] = {
            'file': 'fork-cd-server.yml',
            'enforcement': 'required',
            'reason': 'server CD',
            'required_jobs': ['Fork macOS native contracts'],
        }
        harness.policy['required_checks'].append('Fork macOS native contracts')
        self.assertFails(harness, 'is declared by both')

    def test_required_checks_must_match_the_keep_section(self):
        harness = self.harness()
        harness.policy['required_checks'] = ['Fork gate (Server OS + Cloudflare)']
        self.assertFails(harness, 'required_checks must be exactly the required jobs')

    def test_a_quarantined_workflow_cannot_gate_main(self):
        harness = self.harness()
        harness.policy['quarantine'].append(
            {'file': 'fork-checks.yml', 'reason': 'flaky', 'tracking': 'dev/unified-main/05-ci-matrix.md'}
        )
        self.assertFails(harness, 'a quarantined workflow cannot contribute required checks')

    def test_quarantine_needs_a_resolvable_tracking_pointer(self):
        harness = self.harness()
        harness.policy['workflows']['keep'][1] = {
            'file': 'fork-release-prepare.yml',
            'enforcement': 'advisory',
            'reason': 'freeze lane',
        }
        harness.policy['quarantine'].append(
            {'file': 'fork-release-prepare.yml', 'reason': 'broken', 'tracking': 'dev/does-not-exist.md'}
        )
        self.assertFails(harness, 'is neither an issue URL nor an existing repository file')

    def test_deploy_brand_must_be_a_real_brand(self):
        harness = self.harness()
        harness.policy['deploy_brand'] = 'not-a-brand'
        self.assertFails(harness, "'not-a-brand' is not a brand manifest id")

    def test_the_regression_brand_can_never_deploy(self):
        harness = self.harness()
        harness.policy['deploy_brand'] = 'omi-upstream'
        self.assertFails(harness, 'regression brand and must never be the deploy brand')

    def test_a_deploy_path_naming_another_brand_fails(self):
        harness = self.harness()
        (harness.root / '.github' / 'workflows' / 'fork-cd-server.yml').write_text(
            CD.format(target='server').replace('fork-cd-server-eddy', 'fork-cd-server-omi-upstream'),
            encoding='utf-8',
        )
        self.assertFails(harness, "names 'omi-upstream', which is not the deploy brand")

    def test_a_brand_dispatch_input_is_refused(self):
        harness = self.harness()
        (harness.root / '.github' / 'workflows' / 'fork-release-prepare.yml').write_text(
            PREPARE.replace(
                '      stage:\n',
                '      brand:\n        required: true\n        type: string\n      stage:\n',
            ),
            encoding='utf-8',
        )
        self.assertFails(harness, 'the brand dispatch input must be removed')

    def test_environment_branch_policy_must_be_main_only(self):
        harness = self.harness()
        harness.policy['environments'][0]['branches'] = ['*']
        self.assertFails(harness, "deployment branch policy must be exactly ['main']")


    def test_a_required_workflow_must_report_on_pull_request(self):
        # A required check whose workflow has no pull_request trigger is never
        # reported, so GitHub holds every pull request at "Expected" forever.
        harness = self.harness()
        (harness.root / '.github' / 'workflows' / 'fork-checks.yml').write_text(
            FORK_CHECKS.replace('  pull_request:\n', ''), encoding='utf-8'
        )
        self.assertFails(harness, 'does not trigger on pull_request')

    def test_a_path_filtered_required_workflow_is_refused(self):
        harness = self.harness()
        (harness.root / '.github' / 'workflows' / 'fork-checks.yml').write_text(
            FORK_CHECKS.replace('  pull_request:\n', '  pull_request:\n    paths: ["backend/**"]\n'),
            encoding='utf-8',
        )
        self.assertFails(harness, 'filters pull_request by paths')

    def test_skipped_required_jobs_are_still_reportable(self):
        # The upstream jobs are gated inside the workflow, which GitHub records
        # as a skipped check and treats as satisfied; only the workflow-level
        # trigger decides whether a check is reported at all.
        harness = self.harness()
        (harness.root / '.github' / 'workflows' / 'fork-checks.yml').write_text(
            FORK_CHECKS.replace(
                '  gate:\n',
                '  gate:\n    if: github.event_name == \'push\'\n',
            ),
            encoding='utf-8',
        )
        result = self.check(harness)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class LiveReportTests(unittest.TestCase):
    def harness(self) -> RepoStateHarness:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return RepoStateHarness(Path(tmp.name))

    def live(self, harness: RepoStateHarness, states: dict[str, str], *, published=None) -> list[str]:
        module = load_module()
        on_default_branch = list(states) if published is None else list(published)

        def github(path, repository):
            if path.startswith('contents/'):
                return [{'name': name} for name in on_default_branch]
            return {
                'workflows': [
                    {'path': f'.github/workflows/{name}', 'state': state}
                    for name, state in sorted(states.items())
                ]
            }

        module.github = github
        return module.live_report(harness.policy, harness.root, 'fixture/repo')

    def test_a_workflow_this_change_adds_is_not_reported_as_missing(self):
        # GitHub registers a workflow only once its file reaches the default
        # branch, so a pull request that adds one would fail its own drift check
        # without this distinction.
        harness = self.harness()
        policy = copy.deepcopy(harness.policy)
        policy['workflows']['keep'].append(
            {'file': 'new-tool.yml', 'enforcement': 'advisory', 'reason': 'one-time tool'}
        )
        states = {
            'fork-checks.yml': 'active',
            'fork-release-prepare.yml': 'active',
            'fork-cd-cloudflare.yml': 'active',
            'fork-cd-server.yml': 'active',
            'upstream.yml': 'disabled_manually',
        }
        module = load_module()
        module.github = lambda path, repository: (
            [{'name': name} for name in states]
            if path.startswith('contents/')
            else {'workflows': [{'path': f'.github/workflows/{n}', 'state': s} for n, s in sorted(states.items())]}
        )
        self.assertEqual(module.live_report(policy, harness.root, 'fixture/repo'), [])

    def test_a_declared_workflow_missing_from_the_default_branch_is_still_reported(self):
        harness = self.harness()
        policy = copy.deepcopy(harness.policy)
        policy['workflows']['keep'].append(
            {'file': 'vanished.yml', 'enforcement': 'advisory', 'reason': 'x'}
        )
        (harness.root / '.github' / 'workflows' / 'vanished.yml').write_text(
            'name: V\non:\n  workflow_dispatch:\n', encoding='utf-8'
        )
        states = {
            'fork-checks.yml': 'active',
            'fork-release-prepare.yml': 'active',
            'fork-cd-cloudflare.yml': 'active',
            'fork-cd-server.yml': 'active',
            'upstream.yml': 'disabled_manually',
        }
        module = load_module()
        module.github = lambda path, repository: (
            [{'name': name} for name in [*states, 'vanished.yml']]
            if path.startswith('contents/')
            else {'workflows': [{'path': f'.github/workflows/{n}', 'state': s} for n, s in sorted(states.items())]}
        )
        errors = module.live_report(policy, harness.root, 'fixture/repo')
        self.assertEqual(errors, ['vanished.yml: declared as keep or disable but not registered on GitHub'])

    def test_matching_state_passes(self):
        harness = self.harness()
        errors = self.live(
            harness,
            {
                'fork-checks.yml': 'active',
                'fork-release-prepare.yml': 'active',
                'fork-cd-cloudflare.yml': 'active',
                'fork-cd-server.yml': 'active',
                'upstream.yml': 'disabled_manually',
            },
        )
        self.assertEqual(errors, [])

    def test_a_kept_workflow_that_github_disabled_is_reported(self):
        harness = self.harness()
        errors = self.live(
            harness,
            {
                'fork-checks.yml': 'disabled_manually',
                'fork-release-prepare.yml': 'active',
                'fork-cd-cloudflare.yml': 'active',
                'fork-cd-server.yml': 'active',
                'upstream.yml': 'disabled_manually',
            },
        )
        self.assertEqual(errors, ["fork-checks.yml: declared keep but GitHub reports state='disabled_manually'"])

    def test_an_undeclared_registered_workflow_is_reported(self):
        harness = self.harness()
        errors = self.live(
            harness,
            {
                'fork-checks.yml': 'active',
                'fork-release-prepare.yml': 'active',
                'fork-cd-cloudflare.yml': 'active',
                'fork-cd-server.yml': 'active',
                'upstream.yml': 'disabled_manually',
                'brand-new-upstream.yml': 'active',
            },
        )
        self.assertEqual(errors, ['brand-new-upstream.yml: registered on GitHub but not declared as keep or disable'])

    def test_a_declared_workflow_github_does_not_register_is_reported(self):
        harness = self.harness()
        policy = copy.deepcopy(harness.policy)
        policy['workflows']['keep'].append({'file': 'ghost.yml', 'enforcement': 'advisory', 'reason': 'x'})
        (harness.root / '.github' / 'workflows' / 'ghost.yml').write_text('name: G\non:\n  workflow_dispatch:\n', encoding='utf-8')
        module = load_module()
        module.github = lambda path, repository: {
            'workflows': [
                {'path': f'.github/workflows/{name}', 'state': state}
                for name, state in {
                    'fork-checks.yml': 'active',
                    'fork-release-prepare.yml': 'active',
                    'fork-cd-cloudflare.yml': 'active',
                    'fork-cd-server.yml': 'active',
                    'upstream.yml': 'disabled_manually',
                }.items()
            ]
        }
        errors = module.live_report(policy, harness.root, 'fixture/repo')
        self.assertEqual(errors, ['ghost.yml: declared as keep or disable but not registered on GitHub'])


class GithubErrorTests(unittest.TestCase):
    """The live lane reads GitHub through `gh`, so its failures must be actionable."""

    def call_with_stderr(self, stderr: str):
        module = load_module()
        original = module.subprocess.run
        module.subprocess.run = lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout='', stderr=stderr
        )
        try:
            with self.assertRaises(module.Failure) as caught:
                module.github('actions/workflows?per_page=100', 'fixture/repo')
        finally:
            module.subprocess.run = original
        return str(caught.exception)

    def test_a_missing_token_names_the_workflow_fix(self):
        # The exact failure the pull-request lane hit on 2026-09-11: the job
        # granted `actions: read` but never put the token in the environment.
        message = self.call_with_stderr(
            'gh: To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN environment variable.'
        )
        self.assertIn('GH_TOKEN', message)
        self.assertIn('github.token', message)

    def test_any_other_api_error_is_reported_verbatim(self):
        message = self.call_with_stderr('gh: Resource not accessible by integration (HTTP 403)')
        self.assertEqual(message, 'gh: Resource not accessible by integration (HTTP 403)')


if __name__ == '__main__':
    unittest.main(verbosity=2)
