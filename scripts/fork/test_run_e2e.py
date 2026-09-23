#!/usr/bin/env python3
"""Exercise E2E selection and environment isolation through real pytest processes."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class E2ERunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='omi-e2e-runner-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runner = self.root / 'scripts/fork/run_e2e.py'
        self.runner.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / 'scripts/fork/run_e2e.py', self.runner)
        self.suite = self.root / 'backend/testing/e2e'
        self.suite.mkdir(parents=True)
        self.receipt = self.root / 'executed'

    def run_e2e(self, *arguments):
        environment = {
            **os.environ,
            'GOOGLE_APPLICATION_CREDENTIALS': '/unreachable/ambient-credential.json',
            'FIRESTORE_EMULATOR_HOST': 'not-the-test-emulator:1234',
            'OMI_DEPLOYMENT_PROFILE': 'wrong-production-profile',
            'OPENAI_API_KEY': 'ambient-key-must-not-reach-child',
            'PYTEST_ADDOPTS': '--invalid-ambient-option',
        }
        return subprocess.run(
            [sys.executable, str(self.runner), *arguments],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_flags_keep_suite_scope_and_drop_ambient_provider_state(self):
        (self.root / 'backend/test_outside.py').write_text('raise RuntimeError("collected outside the E2E suite")\n')
        (self.suite / 'test_cases.py').write_text(
            'import os\nfrom pathlib import Path\n'
            'def test_contract():\n'
            '    for key in ("GOOGLE_APPLICATION_CREDENTIALS", "FIRESTORE_EMULATOR_HOST", '
            '"OMI_DEPLOYMENT_PROFILE", "OPENAI_API_KEY", "PYTEST_ADDOPTS"):\n'
            '        assert key not in os.environ\n'
            '    assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"\n'
            f'    Path({str(self.receipt)!r}).write_text("exercised")\n'
            'def test_unselected():\n'
            '    assert False, "-k filter was not applied"\n'
        )
        result = self.run_e2e('-q', '--tb=line', '-k', 'test_contract')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.receipt.is_file(), 'the selected test never ran')

    def test_real_test_failure_is_not_reported_as_success(self):
        (self.suite / 'test_failure.py').write_text('def test_failure():\n    assert False\n')
        result = self.run_e2e('-q', '--tb=line')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
