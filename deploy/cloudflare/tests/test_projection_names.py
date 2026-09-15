#!/usr/bin/env python3
"""Pin the projection-name ratchet: it must catch unbound references and not invent them.

The check exists because a stager that selects a node without a helper the node calls emits a
module that raises `NameError` on a path nothing in the lane exercises -- `_budget_authority`
cost ten days of invisibility. Its two failure modes are symmetrical and both are tested here:
missing a genuinely unbound name, and reporting a name a star import or a recorded baseline
already explains.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / 'deploy/cloudflare/scripts/check_projection_names.py'


def checker():
    spec = importlib.util.spec_from_file_location('check_projection_names', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UnboundReferenceTests(unittest.TestCase):
    def staged(self, **modules):
        return {name: ast.parse(text) for name, text in modules.items()}

    def test_a_reference_nothing_binds_is_reported(self):
        staged = self.staged(
            **{
                'runtime.py': (
                    'def helper():\n    return 1\n' 'def caller():\n    return helper() + _missing_helper()\n'
                )
            }
        )
        self.assertEqual(checker().unbound_references(staged), [('runtime.py', '_missing_helper')])

    def test_a_star_import_explains_a_reference_it_provides(self):
        staged = self.staged(
            **{
                'policy.py': 'def chosen():\n    return MAX_TURNS\nMAX_TURNS = 3\n',
                'runtime.py': 'from policy import *\n\ndef caller():\n    return chosen()\n',
            }
        )
        self.assertEqual(checker().unbound_references(staged), [])

    def test_an_unresolvable_star_import_suppresses_the_module(self):
        # `from langchain_core.messages import *` may provide anything, so a module that has
        # one cannot be judged from its own text.
        staged = self.staged(**{'runtime.py': 'from third_party import *\n\ndef caller():\n    return Unknown()\n'})
        self.assertEqual(checker().unbound_references(staged), [])

    def test_a_locally_bound_name_is_not_a_module_reference(self):
        staged = self.staged(
            **{
                'runtime.py': (
                    'def caller(value):\n'
                    '    local = value + 1\n'
                    '    def inner():\n'
                    '        return local\n'
                    '    return inner()\n'
                )
            }
        )
        self.assertEqual(checker().unbound_references(staged), [])


class RatchetTests(unittest.TestCase):
    """The comparison half: a recorded entry does not block, a new one does."""

    def run_check(self, baseline_entries):
        with tempfile.TemporaryDirectory(prefix='cf-projection-names-test-') as directory:
            baseline = Path(directory) / 'baseline.json'
            baseline.write_text(json.dumps({'schema_version': 1, 'known': baseline_entries}))
            return subprocess.run(
                [sys.executable, str(SCRIPT), '--baseline', str(baseline)],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_the_current_tree_matches_its_baseline(self):
        completed = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn('no new ones', completed.stdout)

    def test_a_new_unbound_reference_fails_against_the_recorded_floor(self):
        completed = self.run_check([])
        self.assertEqual(completed.returncode, 1)
        self.assertIn('new unbound projection reference(s)', completed.stdout)

    def test_a_recorded_entry_without_a_reason_fails(self):
        baseline = json.loads((ROOT / 'deploy/cloudflare/projection-names.baseline.json').read_text())
        baseline['known'][0]['reason'] = ''
        completed = self.run_check(baseline['known'])
        self.assertEqual(completed.returncode, 1)
        self.assertIn('have no reason', completed.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
