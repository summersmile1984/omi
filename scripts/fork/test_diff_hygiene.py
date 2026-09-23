#!/usr/bin/env python3
"""Real Git regressions for the upstream-import/whitespace ownership boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / 'scripts/fork/check_diff_hygiene.py'


class DiffHygieneTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='omi-fork-hygiene-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        self.environment.update({'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull})
        self.git('init', '-q')
        self.source = self.root / 'source.py'
        self.source.write_text('value = 1\n\n')
        self.commit()
        self.upstream = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('update-ref', 'refs/remotes/upstream/main', self.upstream)
        self.source.write_text('value = 1\n')
        self.commit()
        self.base = self.git('rev-parse', 'HEAD').stdout.strip()
        self.files = self.root / 'changed-paths'
        self.files.write_text('source.py\n')

    def git(self, *arguments):
        return subprocess.run(
            ['git', *arguments],
            cwd=self.root,
            env=self.environment,
            text=True,
            capture_output=True,
            check=True,
        )

    def commit(self):
        self.git('add', '.')
        self.git(
            '-c',
            'user.name=Fixture',
            '-c',
            'user.email=fixture@example.invalid',
            '-c',
            'core.hooksPath=/dev/null',
            'commit',
            '-qm',
            'fixture',
        )

    def run_check(self, *arguments):
        return subprocess.run(
            [sys.executable, str(CHECKER), '--base', self.base, '--changed-files', str(self.files), *arguments],
            cwd=self.root,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_restoring_upstream_eof_passes_worktree_and_committed_checks(self):
        self.source.write_text('value = 1\n\n')
        self.assertEqual(self.run_check().returncode, 0)
        self.commit()
        old_check = subprocess.run(
            ['git', 'diff', '--check', self.base, 'HEAD'],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(old_check.returncode, 0)
        checked = self.run_check('--head', self.git('rev-parse', 'HEAD').stdout.strip())
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_new_whitespace_in_upstream_file_is_still_rejected(self):
        self.source.write_text('value = 2 \n\n')
        self.assertEqual(self.run_check().returncode, 1)

    def test_fork_whitespace_rejected_before_and_after_commit(self):
        path = self.root / 'fork.py'
        path.write_text('value = 2 \n')
        self.files.write_text('fork.py\n')
        self.assertEqual(self.run_check().returncode, 1)
        self.commit()
        self.assertEqual(self.run_check('--head', self.git('rev-parse', 'HEAD').stdout.strip()).returncode, 1)

    def test_upstream_unchanged_conflict_marker_is_not_exempt(self):
        self.source.write_text('<<<<<<< unresolved\n')
        self.commit()
        self.git('update-ref', 'refs/remotes/upstream/main', 'HEAD')
        checked = self.run_check()
        self.assertEqual(checked.returncode, 1)
        self.assertIn('source.py:1:', checked.stderr)

    def test_unavailable_upstream_cannot_pass(self):
        self.assertEqual(self.run_check('--upstream-ref', 'refs/remotes/missing/main').returncode, 2)

    def test_later_unmerged_upstream_cannot_hide_bad_fork_whitespace(self):
        self.source.write_text('value = 2 \n')
        self.commit()
        fork_head = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('checkout', '--detach', self.upstream)
        self.source.write_text('value = 2 \n')
        self.commit()
        self.git('update-ref', 'refs/remotes/upstream/main', 'HEAD')
        self.git('checkout', '--detach', fork_head)
        self.assertEqual(self.run_check().returncode, 1)

    def test_adapted_manifest_preserves_other_check_failures(self):
        for relative in (
            'scripts/fork/upstream_checks.py',
            'scripts/fork/check_diff_hygiene.py',
            '.github/scripts/run_checks.py',
            '.github/scripts/git_bash.py',
        ):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        original = '.github/scripts/check_diff_hygiene.py'
        (self.root / original).write_text('raise SystemExit(99)\n')
        checks = [
            {
                'id': 'diff-hygiene',
                'command': [
                    'python3',
                    original,
                    '--changed-files',
                    '{changed_files}',
                    '--base',
                    '{base}',
                    '--head',
                    '{head}',
                ],
            },
            {'id': 'other-contract', 'command': ['bash', '-c', 'exit 17']},
        ]
        manifest = ['checks:']
        for check in checks:
            check.update(triggers=['all'], lanes=['local', 'ci'], reason='fixture')
            for index, (key, value) in enumerate(check.items()):
                manifest.append(('  - ' if index == 0 else '    ') + key + ': ' + json.dumps(value))
        manifest_path = self.root / '.github/checks-manifest.yaml'
        original_manifest = '\n'.join(manifest) + '\n'
        manifest_path.write_text(original_manifest)
        self.source.write_text('value = 1\n\n')
        # Avoid testing the incidental PATH interpreter, not the selected interpreter.
        shim = self.root / 'bin'
        shim.mkdir()
        (shim / 'python3').symlink_to(sys.executable)
        environment = {**self.environment, 'PATH': f'{shim}{os.pathsep}{os.environ.get("PATH", os.defpath)}'}
        result = subprocess.run(
            [
                sys.executable,
                str(self.root / 'scripts/fork/upstream_checks.py'),
                '--lane',
                'local',
                '--base',
                self.base,
                '--changed-files',
                str(self.files),
            ],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('PASS diff-hygiene', result.stdout)
        self.assertIn('FAIL other-contract', result.stdout)
        self.assertEqual(manifest_path.read_text(), original_manifest)


if __name__ == '__main__':
    unittest.main(verbosity=2)
