#!/usr/bin/env python3
"""Execute the actual fork workflow shell against disposable Git histories."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/fork-checks.yml"


class DiffBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="fork-ci-base-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q", "-b", "main")
        self.commit("base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("branch", "fixture-base")
        self.commit("current")
        self.git("remote", "add", "origin", str(self.root))
        workflow = yaml.safe_load(WORKFLOW.read_text())
        self.command = next(step["run"] for step in workflow["jobs"]["fork-gate"]["steps"] if step.get("id") == "base")

    def git(self, *arguments: str) -> str:
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", *arguments], cwd=self.root, text=True, stderr=subprocess.PIPE
        ).strip()

    def commit(self, text: str) -> None:
        (self.root / "file").write_text(text)
        self.git("add", "file")
        self.git("-c", "user.name=CI Fixture", "-c", "user.email=ci@example.invalid", "commit", "-qm", text)

    def resolve(self, event: str, before: str = "", base: str = "") -> str:
        output = self.root / "github-output"
        environment = {
            **os.environ,
            "EVENT_NAME": event,
            "BEFORE_SHA": before,
            "BASE_REF": base,
            "GITHUB_OUTPUT": str(output),
        }
        subprocess.run(
            ["bash", "-euo", "pipefail", "-c", self.command],
            cwd=self.root,
            env=environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return output.read_text().strip()

    def test_manual_dispatch_without_before_uses_current_commit_parent(self) -> None:
        self.assertEqual(self.resolve("workflow_dispatch"), f"ref={self.base}")

    def test_push_uses_the_entire_pushed_range(self) -> None:
        self.commit("second pushed commit")
        self.assertEqual(self.resolve("push", before=self.base), f"ref={self.base}")

    def test_new_branch_zero_before_has_a_real_base(self) -> None:
        self.assertEqual(self.resolve("push", before="0" * 40), f"ref={self.base}")

    def test_pull_request_fetches_its_base_branch(self) -> None:
        self.assertEqual(self.resolve("pull_request", base="fixture-base"), f"ref={self.base}")

    def test_unavailable_push_commit_fails_before_emitting_a_base(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            self.resolve("push", before="f" * 40)
        self.assertFalse((self.root / "github-output").exists())


if __name__ == "__main__":
    unittest.main()
