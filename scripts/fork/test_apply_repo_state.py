#!/usr/bin/env python3
"""Behavioral tests for the repository settings applier.

The API surface is not exercised: these tests cover the payload it sends and the
comparison it uses to decide whether the repository matches the policy, which is
where a wrong answer would be silent.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'apply_repo_state.py'


def load_module():
    spec = importlib.util.spec_from_file_location('apply_repo_state', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def policy() -> dict:
    return {
        'schema_version': 1,
        'deploy_brand': 'eddy',
        'workflows': {
            'keep': [
                {'file': 'fork-checks.yml', 'enforcement': 'required', 'reason': 'gate',
                 'required_jobs': ['Fork gate']},
                {'file': 'web-checks.yml', 'enforcement': 'required', 'reason': 'web',
                 'required_jobs': ['Frontend Lint']},
            ],
            'disable': [{'file': 'gcp_backend.yml', 'reason': 'upstream deploy'}],
            'unregistered': [{'file': 'sdk-rust.yml', 'reason': 'never registered'}],
        },
        'quarantine': [],
        'required_checks': ['Fork gate', 'Frontend Lint'],
        'environments': [
            {'name': 'cloudflare-beta', 'branches': ['main'], 'reviewers': []},
            {'name': 'cloudflare-production', 'branches': ['main'], 'reviewers': ['summersmile1984']},
        ],
    }


def environment_state(reviewers: int = 0) -> dict:
    """The real response shape: reviewers live inside protection_rules."""
    rules = [{'type': 'branch_policy'}]
    if reviewers:
        rules.append({'type': 'required_reviewers', 'reviewers': [{'type': 'User', 'id': 1}] * reviewers})
    return {'name': 'cloudflare-beta', 'protection_rules': rules}


class RulesetPayloadTests(unittest.TestCase):
    def test_every_declared_check_becomes_a_required_context(self):
        payload = load_module().ruleset_payload(policy())
        self.assertEqual(payload['conditions']['ref_name']['include'], ['refs/heads/main'])
        self.assertEqual(payload['enforcement'], 'active')
        rule = next(rule for rule in payload['rules'] if rule['type'] == 'required_status_checks')
        contexts = [entry['context'] for entry in rule['parameters']['required_status_checks']]
        self.assertEqual(contexts, policy()['required_checks'])
        # Branch freshness is not required: it would serialize every merge
        # behind a full run of the slowest lane.
        self.assertFalse(rule['parameters']['strict_required_status_checks_policy'])

    def test_the_ruleset_protects_main_and_keeps_a_break_glass(self):
        payload = load_module().ruleset_payload(policy())
        kinds = {rule['type'] for rule in payload['rules']}
        self.assertIn('deletion', kinds)
        self.assertIn('non_fast_forward', kinds)
        self.assertIn('pull_request', kinds)
        self.assertEqual(payload['bypass_actors'][0]['bypass_mode'], 'always')


class PlanTests(unittest.TestCase):
    def test_an_in_sync_repository_only_needs_the_ruleset_and_environments(self):
        steps = load_module().plan(policy(), {'fork-checks.yml': 'active', 'web-checks.yml': 'active',
                                              'gcp_backend.yml': 'disabled_manually'})
        self.assertEqual([step for step in steps if 'workflow' in step], [])

    def test_drifted_workflow_states_are_planned(self):
        steps = load_module().plan(policy(), {'fork-checks.yml': 'disabled_manually',
                                              'web-checks.yml': 'active',
                                              'gcp_backend.yml': 'active'})
        self.assertIn('enable workflow fork-checks.yml', steps)
        self.assertIn('disable workflow gcp_backend.yml', steps)
        self.assertNotIn('enable workflow web-checks.yml', steps)


class ApplyStepsTests(unittest.TestCase):
    """Only the ruleset waits for the required checks; the rest is independent."""

    def in_sync(self) -> dict:
        return {'fork-checks.yml': 'active', 'web-checks.yml': 'active', 'gcp_backend.yml': 'disabled_manually'}

    def test_a_blocked_ruleset_does_not_hold_back_the_other_work(self):
        ready, waiting = load_module().apply_steps(policy(), self.in_sync(), ['Fork gate (failure)'])
        self.assertEqual([step for step in ready if step.startswith('ruleset')], [])
        self.assertEqual([step for step in ready if step.startswith('set environment')], [
            "set environment 'cloudflare-beta' to branches ['main']",
            "set environment 'cloudflare-production' to branches ['main']",
        ])
        self.assertEqual(waiting, ["ruleset 'fork-main-gate': waiting on Fork gate (failure)"])

    def test_an_unblocked_ruleset_is_ready(self):
        ready, waiting = load_module().apply_steps(policy(), self.in_sync(), [])
        self.assertEqual(waiting, [])
        self.assertIn("ruleset 'fork-main-gate' requiring: Fork gate, Frontend Lint", ready)


class LiveEnvironmentBranchTests(unittest.TestCase):
    def test_an_environment_without_a_branch_policy_reads_as_empty(self):
        # GitHub answers 404 for this endpoint when no custom branch policy
        # exists, which is a real state (every branch is allowed) and must be
        # comparable rather than fatal.
        module = load_module()
        module.api = lambda repository, path, **kwargs: None
        self.assertEqual(module.live_environment_branches('fixture/repo', 'development'), [])

    def test_no_branch_policy_does_not_satisfy_a_main_only_declaration(self):
        module = load_module()
        errors = module.verify_state(
            policy(),
            module.ruleset_payload(policy()),
            {'cloudflare-beta': environment_state(0), 'cloudflare-production': environment_state(1)},
            {'cloudflare-beta': [], 'cloudflare-production': ['main']},
        )
        self.assertIn("environment 'cloudflare-beta' allows [], policy declares ['main']", errors)


class VerifyStateTests(unittest.TestCase):
    def clean(self) -> tuple:
        module = load_module()
        return module, module.ruleset_payload(policy())

    def test_a_matching_state_reports_nothing(self):
        module, ruleset = self.clean()
        errors = module.verify_state(
            policy(),
            ruleset,
            {'cloudflare-beta': environment_state(0), 'cloudflare-production': environment_state(1)},
            {'cloudflare-beta': ['main'], 'cloudflare-production': ['main']},
        )
        self.assertEqual(errors, [])

    def test_a_missing_ruleset_is_reported(self):
        module, _ = self.clean()
        errors = module.verify_state(policy(), None, {}, {})
        self.assertIn("ruleset 'fork-main-gate' is missing from the repository", errors)

    def test_a_ruleset_without_a_required_check_is_reported(self):
        module, ruleset = self.clean()
        trimmed = copy.deepcopy(ruleset)
        rule = next(rule for rule in trimmed['rules'] if rule['type'] == 'required_status_checks')
        rule['parameters']['required_status_checks'] = [{'context': 'Fork gate'}]
        errors = module.verify_state(
            policy(), trimmed,
            {'cloudflare-beta': environment_state(0), 'cloudflare-production': environment_state(1)},
            {'cloudflare-beta': ['main'], 'cloudflare-production': ['main']},
        )
        self.assertIn("ruleset 'fork-main-gate' does not require 'Frontend Lint'", errors)

    def test_an_undeclared_required_check_is_reported(self):
        module, ruleset = self.clean()
        extra = copy.deepcopy(ruleset)
        rule = next(rule for rule in extra['rules'] if rule['type'] == 'required_status_checks')
        rule['parameters']['required_status_checks'].append({'context': 'Mystery Check'})
        errors = module.verify_state(
            policy(), extra,
            {'cloudflare-beta': environment_state(0), 'cloudflare-production': environment_state(1)},
            {'cloudflare-beta': ['main'], 'cloudflare-production': ['main']},
        )
        self.assertIn(
            "ruleset 'fork-main-gate' requires 'Mystery Check', which the policy does not declare", errors
        )

    def test_a_disabled_or_unbypassable_ruleset_is_reported(self):
        module, ruleset = self.clean()
        for mutate, fragment in (
            (lambda data: data.update(enforcement='disabled'), "enforcement is 'disabled'"),
            (lambda data: data.pop('bypass_actors'), 'has no break-glass bypass actor'),
        ):
            with self.subTest(fragment=fragment):
                broken = copy.deepcopy(ruleset)
                mutate(broken)
                errors = module.verify_state(
                    policy(), broken,
                    {'cloudflare-beta': environment_state(0), 'cloudflare-production': environment_state(1)},
                    {'cloudflare-beta': ['main'], 'cloudflare-production': ['main']},
                )
                self.assertIn(fragment, ' '.join(errors))

    def test_environment_drift_is_reported(self):
        module, ruleset = self.clean()
        errors = module.verify_state(
            policy(),
            ruleset,
            {'cloudflare-beta': environment_state(1), 'cloudflare-production': environment_state(0)},
            {'cloudflare-beta': ['main', 'release/*'], 'cloudflare-production': ['main']},
        )
        self.assertIn("environment 'cloudflare-beta' allows ['main', 'release/*'], policy declares ['main']", errors)
        self.assertIn("environment 'cloudflare-beta' has 1 required reviewer(s), policy declares 0", errors)
        self.assertIn("environment 'cloudflare-production' has 0 required reviewer(s), policy declares 1", errors)

    def test_a_missing_environment_is_reported(self):
        module, ruleset = self.clean()
        errors = module.verify_state(policy(), ruleset, {}, {})
        self.assertIn("environment 'cloudflare-beta' is missing from the repository", errors)

    def test_reviewers_are_read_from_the_protection_rules(self):
        # The environment response has no top-level `reviewers` field; reading
        # one reports zero for an environment that has them, which is how the
        # first real `--apply` looked like it had failed.
        module = load_module()
        self.assertEqual(module.required_reviewers({'protection_rules': [{'type': 'branch_policy'}]}), [])
        reviewers = module.required_reviewers(
            {'protection_rules': [{'type': 'required_reviewers', 'reviewers': [{'id': 7}]}]}
        )
        self.assertEqual(reviewers, [{'id': 7}])
        # A top-level field that GitHub does not send must not be believed.
        self.assertEqual(module.required_reviewers({'reviewers': [{'id': 7}]}), [])


class ArmableTests(unittest.TestCase):
    def armable(self, states: dict[str, str | None], policy_document: dict) -> list[str]:
        module = load_module()
        module.api = lambda repository, path, **kwargs: (
            {'workflow_runs': [{'id': 1}]}
            if path.endswith('runs?branch=main&per_page=1')
            else {'jobs': [{'name': name, 'conclusion': conclusion} for name, conclusion in states.items()]}
        )
        return module.armable(policy_document, 'fixture/repo')

    def test_a_failing_required_job_blocks_arming_the_ruleset(self):
        blocking = self.armable({'Fork gate': 'failure', 'Frontend Lint': 'success'}, policy())
        self.assertEqual(blocking, ['Fork gate (failure)'])

    def test_a_skipped_path_scoped_job_does_not_block(self):
        # The upstream jobs are path-scoped and are routinely skipped on a push
        # that does not touch their paths; treating that as breakage would make
        # the ruleset impossible to arm.
        blocking = self.armable({'Fork gate': 'success', 'Frontend Lint': 'skipped'}, policy())
        self.assertEqual(blocking, [])

    def test_a_missing_job_is_reported_rather_than_assumed_green(self):
        blocking = self.armable({'Fork gate': 'success'}, policy())
        self.assertEqual(blocking, ['Frontend Lint (not reported)'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
