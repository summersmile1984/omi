#!/usr/bin/env python3
"""Pin how the fork gate reports failures: every selected check runs, all are reported.

The upstream runner stops at the first failing check, so the fork wrapper fans out one check
per invocation. Two things are easy to get wrong here and both cost a full CI round trip per
hidden failure: dropping the fan-out (back to first-failure-only), and leaving a caller's own
`--check-id` in the base invocation (the upstream runner takes a list, so that check would
run again on every round).
"""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]


def wrapper():
    spec = importlib.util.spec_from_file_location('fork_run_checks', ROOT / 'scripts/fork/run_checks.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FanOutTests(unittest.TestCase):
    def setUp(self):
        self.module = wrapper()

    def execute(self, check_ids, failing):
        attempted = []

        def run(command, cwd=None):  # noqa: ARG001 - stands in for subprocess.call
            attempted.append(command[-1])
            return 1 if command[-1] in failing else 0

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.module.execute_each(['runner', '--lane', 'ci'], check_ids, run=run)
        return code, attempted, out.getvalue(), err.getvalue()

    def test_one_failure_does_not_hide_the_checks_after_it(self):
        code, attempted, _, err = self.execute(['fork-a', 'fork-b', 'fork-c'], {'fork-a', 'fork-c'})
        self.assertEqual(attempted, ['fork-a', 'fork-b', 'fork-c'])
        self.assertEqual(code, 1)
        self.assertIn('fork-a, fork-c', err)

    def test_a_clean_run_attempts_every_check_and_passes(self):
        code, attempted, out, err = self.execute(['fork-a', 'fork-b'], set())
        self.assertEqual(attempted, ['fork-a', 'fork-b'])
        self.assertEqual(code, 0)
        self.assertEqual(err, '')
        self.assertIn('passed: 2 check(s)', out)

    def test_each_check_runs_once_with_only_its_own_id(self):
        attempted = []

        def run(command, cwd=None):  # noqa: ARG001
            attempted.append(command[command.index('--check-id') + 1])
            self.assertEqual(command.count('--check-id'), 1)
            return 0

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.module.execute_each(['runner'], ['fork-a', 'fork-b'], run=run)
        self.assertEqual(attempted, ['fork-a', 'fork-b'])


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.module = wrapper()

    def test_a_caller_supplied_check_id_is_read_in_both_spellings(self):
        arguments = ['--lane', 'ci', '--check-id', 'fork-a', '--check-id=fork-b', '--platform', 'linux']
        self.assertEqual(self.module.explicit_ids(arguments), ['fork-a', 'fork-b'])

    def test_the_base_invocation_carries_no_check_id_to_repeat(self):
        arguments = ['--lane', 'ci', '--check-id', 'fork-a', '--check-id=fork-b', '--platform', 'linux']
        self.assertEqual(self.module.without_check_ids(arguments), ['--lane', 'ci', '--platform', 'linux'])

    def test_stripping_keeps_the_argument_pairs_aligned(self):
        # A value that looks like a flag still belongs to the flag before it, so removing
        # the pair must not shift the remaining arguments.
        arguments = ['--lane', 'ci', '--check-id', 'fork-a', '--platform', 'linux', '--base', 'origin/main']
        self.assertEqual(
            self.module.without_check_ids(arguments), ['--lane', 'ci', '--platform', 'linux', '--base', 'origin/main']
        )

    def test_a_dangling_check_id_never_crashes_the_wrapper(self):
        self.assertEqual(self.module.explicit_ids(['--lane', 'ci', '--check-id']), [])
        self.assertEqual(self.module.without_check_ids(['--lane', 'ci', '--check-id']), ['--lane', 'ci'])

    def test_selection_probes_stay_single_shot(self):
        for arguments in (
            ['--lane', 'ci', '--output', 'json'],
            ['--lane', 'ci', '--output=json'],
            ['--lane', 'ci', '--list'],
            ['--lane', 'ci', '--metadata-only'],
        ):
            with self.subTest(arguments=arguments):
                self.assertTrue(self.module.resolves_only(arguments))
        self.assertFalse(self.module.resolves_only(['--lane', 'ci']))


class SkipTests(unittest.TestCase):
    def setUp(self):
        self.module = wrapper()

    @contextmanager
    def skip_env(self, value):
        previous = os.environ.pop('FORK_SKIP_CHECKS', None)
        if value is not None:
            os.environ['FORK_SKIP_CHECKS'] = value
        try:
            yield
        finally:
            os.environ.pop('FORK_SKIP_CHECKS', None)
            if previous is not None:
                os.environ['FORK_SKIP_CHECKS'] = previous

    def run_entry_point(self, arguments, extra_env):
        environment = {
            key: value for key, value in os.environ.items() if key not in ('FORK_FULL_CHECKS', 'FORK_SKIP_CHECKS')
        }
        environment.update(extra_env)
        return subprocess.run(
            [sys.executable, str(ROOT / 'scripts/fork/run_checks.py'), *arguments],
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
        )

    def test_without_the_variable_the_whole_selection_runs(self):
        with self.skip_env(None):
            self.assertEqual(self.module.skip_check_ids(), [])
        retained, excluded = self.module.without_skipped(['fork-a', 'fork-b'], [])
        self.assertEqual((retained, excluded), (['fork-a', 'fork-b'], []))

    def test_only_the_named_checks_are_excluded_and_order_is_kept(self):
        selection = ['fork-selfhost-product-core', 'fork-a', 'fork-b']
        retained, excluded = self.module.without_skipped(selection, ['fork-selfhost-product-core'])
        self.assertEqual(retained, ['fork-a', 'fork-b'])
        self.assertEqual(excluded, ['fork-selfhost-product-core'])

    def test_the_variable_parses_commas_whitespace_and_blanks(self):
        for value, expected in (
            (None, []),
            ('', []),
            ('  ', []),
            ('fork-selfhost-product-core', ['fork-selfhost-product-core']),
            (' fork-a , fork-b ,, ', ['fork-a', 'fork-b']),
        ):
            with self.subTest(value=value), self.skip_env(value):
                self.assertEqual(self.module.skip_check_ids(), expected)

    def test_the_complete_lane_refuses_to_skip_any_check(self):
        result = self.run_entry_point(
            ['--lane', 'ci', '--output', 'json'],
            {'FORK_FULL_CHECKS': 'true', 'FORK_SKIP_CHECKS': 'fork-selfhost-product-core'},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('FORK_SKIP_CHECKS', result.stderr)
        self.assertIn('FORK_FULL_CHECKS=true', result.stderr)

    def test_a_skipped_check_is_never_executed(self):
        # The explicit id stands in for the workflow's selection; with the skip
        # variable set the wrapper must report the exclusion and run nothing,
        # rather than executing the check or failing.
        result = self.run_entry_point(
            ['--lane', 'ci', '--check-id', 'fork-selfhost-product-core'],
            {'FORK_SKIP_CHECKS': 'fork-selfhost-product-core'},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('skipping 1 check(s): fork-selfhost-product-core', result.stderr)
        self.assertIn('passed: 0 check(s)', result.stdout)

    def test_selection_probes_stay_unaffected_by_the_skip_variable(self):
        # The workflow's provisioning decisions read `--output json`; the skip
        # variable may only filter what is executed, never what is selected.
        # Base on the last commit that touched the workflow, whose diff always
        # triggers fork-selfhost-product-core.
        changed = subprocess.check_output(
            ['git', 'log', '-1', '--format=%H', '--', '.github/workflows/fork-checks.yml'],
            cwd=ROOT,
            text=True,
        ).strip()
        base = subprocess.check_output(['git', 'rev-parse', f'{changed}^'], cwd=ROOT, text=True).strip()
        result = self.run_entry_point(['--lane', 'ci', '--base', base, '--output', 'json'], {'FORK_SKIP_CHECKS': 'x'})
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = [check['id'] for check in json.loads(result.stdout)['checks']]
        self.assertIn('fork-selfhost-product-core', selected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
