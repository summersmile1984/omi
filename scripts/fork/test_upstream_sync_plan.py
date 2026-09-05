#!/usr/bin/env python3
"""Behavioral tests for the non-mutating upstream sync planner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLANNER = Path(__file__).resolve().parent / "upstream_sync_plan.py"


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(cwd)},
    )
    if proc.returncode:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


class UpstreamSyncPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "fork@example.test")
        git(self.root, "config", "user.name", "Fork Test")
        (self.root / "shared.txt").write_text("base\n", encoding="utf-8")
        git(self.root, "add", "shared.txt")
        git(self.root, "commit", "-qm", "base")
        git(self.root, "branch", "base")
        git(self.root, "branch", "upstream/main")

    def plan(self) -> tuple[int, dict[str, object], str]:
        proc = subprocess.run(
            [sys.executable, str(PLANNER), "--base", "base", "--upstream", "upstream/main"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        payload = json.loads(proc.stdout) if proc.stdout else {}
        return proc.returncode, payload, proc.stderr

    def test_clean_plan_reports_no_conflicts_and_never_moves_head(self) -> None:
        git(self.root, "switch", "-q", "upstream/main")
        (self.root / "upstream-only.txt").write_text("new\n", encoding="utf-8")
        git(self.root, "add", "upstream-only.txt")
        git(self.root, "commit", "-qm", "upstream change")
        git(self.root, "switch", "-q", "main")
        before = git(self.root, "rev-parse", "HEAD").strip()

        rc, plan, stderr = self.plan()

        self.assertEqual(rc, 0, stderr)
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(git(self.root, "rev-parse", "HEAD").strip(), before)
        self.assertEqual(git(self.root, "status", "--short"), "")

    def test_conflicting_plan_lists_the_file_without_mutating_head(self) -> None:
        git(self.root, "switch", "-q", "upstream/main")
        (self.root / "shared.txt").write_text("upstream\n", encoding="utf-8")
        git(self.root, "add", "shared.txt")
        git(self.root, "commit", "-qm", "upstream change")
        git(self.root, "switch", "-q", "main")
        (self.root / "shared.txt").write_text("fork\n", encoding="utf-8")
        git(self.root, "add", "shared.txt")
        git(self.root, "commit", "-qm", "fork change")
        git(self.root, "branch", "-f", "base", "HEAD")
        before = git(self.root, "rev-parse", "HEAD").strip()

        rc, plan, stderr = self.plan()

        self.assertEqual(rc, 0, stderr)
        self.assertEqual(plan["conflicts"], ["shared.txt"])
        self.assertEqual(git(self.root, "rev-parse", "HEAD").strip(), before)
        self.assertEqual(git(self.root, "status", "--short"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
