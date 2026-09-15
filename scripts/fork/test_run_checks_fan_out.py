#!/usr/bin/env python3
"""Pin how the fork gate reports failures: every selected check runs, all are reported.

The upstream runner stops at the first failing check, so the fork wrapper fans out one check
per invocation. Two things are easy to get wrong here and both cost a full CI round trip per
hidden failure: dropping the fan-out (back to first-failure-only), and leaving a caller's own
`--check-id` in the base invocation (the upstream runner takes a list, so that check would
run again on every round).
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
