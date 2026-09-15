#!/usr/bin/env python3
"""Exercise dev/git-hygiene.sh in real Git fixtures, including the failure it prevents.

The defect is not cosmetic: one remote-tracking refspec that names a deleted branch makes
`git fetch` fail as a whole, so `git pull` on main reports an error and fetches nothing. The
2026-09-15 branch prune left three such refspecs in this checkout.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
GIT_ENV = {**os.environ, 'LC_ALL': 'C', 'LANG': 'C'}
HYGIENE = REPO_ROOT / "dev" / "git-hygiene.sh"


class GitHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="omi-git-hygiene-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.root)], check=False))
        self.remote = self.root / "remote.git"
        self.clone = self.root / "clone"
        self.git("init", "--bare", "--initial-branch=main", str(self.remote), cwd=self.root)
        self.git("clone", str(self.remote), str(self.clone), cwd=self.root)
        self.git("config", "user.name", "Fixture", cwd=self.clone)
        self.git("config", "user.email", "fixture@example.invalid", cwd=self.clone)
        (self.clone / "file.txt").write_text("one\n")
        self.git("add", "file.txt", cwd=self.clone)
        self.git("commit", "-m", "one", cwd=self.clone)
        self.git("push", "-u", "origin", "main", cwd=self.clone)

    def git(self, *arguments: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
        # LC_ALL=C: git localizes its messages, and the CI runner is not an English host --
        # this test first failed there with 'fatal: 无法找到远程引用 refs/heads/gone'.
        return subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True, check=check, env=GIT_ENV)

    def hygiene(self, mode: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(HYGIENE), mode, "origin"],
            cwd=self.clone,
            capture_output=True,
            text=True,
            check=False,
            env=GIT_ENV,
        )

    def add_dead_refspec(self, branch: str = "gone") -> None:
        self.git(
            "config",
            "--add",
            "remote.origin.fetch",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            cwd=self.clone,
        )

    def test_a_clean_checkout_passes(self) -> None:
        completed = self.hygiene("check")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("OK: no dead refspec", completed.stdout)

    def test_a_dead_refspec_breaks_fetch_and_the_check_names_it(self) -> None:
        self.add_dead_refspec()
        fetch = self.git("fetch", "origin", cwd=self.clone, check=False)
        self.assertNotEqual(fetch.returncode, 0, "the fixture did not reproduce a failing fetch")
        # The ref name, not the sentence: git localizes the surrounding message.
        self.assertIn("refs/heads/gone", fetch.stderr)

        completed = self.hygiene("check")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("+refs/heads/gone:refs/remotes/origin/gone", completed.stdout)

    def test_repair_restores_a_working_fetch(self) -> None:
        self.add_dead_refspec()
        self.assertEqual(self.hygiene("repair").returncode, 0)
        fetch = self.git("fetch", "origin", cwd=self.clone, check=False)
        self.assertEqual(fetch.returncode, 0, fetch.stderr)
        self.assertEqual(self.hygiene("check").returncode, 0)

    def test_repair_removes_tracking_refs_for_deleted_branches(self) -> None:
        self.git("checkout", "-b", "stale", cwd=self.clone)
        (self.clone / "file.txt").write_text("two\n")
        self.git("commit", "-am", "two", cwd=self.clone)
        self.git("push", "-u", "origin", "stale", cwd=self.clone)
        # Delete it on the remote side, the way a branch prune does. Deleting it through the
        # clone would remove the local tracking ref too, and then there is nothing to repair.
        self.git("branch", "-D", "stale", cwd=self.remote)

        def tracking_ref_present() -> bool:
            # Not a path check: git may pack refs, so ask git.
            return (
                self.git("rev-parse", "--verify", "refs/remotes/origin/stale", cwd=self.clone, check=False).returncode
                == 0
            )

        self.assertTrue(tracking_ref_present(), "the fixture did not leave a tracking ref behind")

        self.assertEqual(self.hygiene("check").returncode, 1)
        self.assertEqual(self.hygiene("repair").returncode, 0)
        self.assertFalse(tracking_ref_present())
        self.assertEqual(self.hygiene("check").returncode, 0)

    def test_an_unknown_remote_is_a_usage_error(self) -> None:
        completed = subprocess.run(
            [str(HYGIENE), "check", "not-a-remote"],
            cwd=self.clone,
            capture_output=True,
            text=True,
            check=False,
            env=GIT_ENV,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("not a configured remote", completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
