#!/usr/bin/env python3
"""Behavioral tests for the zero-upstream-touch guard.

Each test builds a throwaway git repository with a real `upstream/main` ref and
a real diff, then runs the guard as a subprocess. That exercises the production
path -- argument parsing, git plumbing, allowlist parsing, exit codes -- rather
than asserting on source text.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parent / "check-upstream-touch.py"

ALLOWLIST = """\
schema_version: 1

forbidden_patterns:
  - "backend/pylock*.toml"

allow:
  - path: seam.swift
    max_added_lines: 3
    reason: the one configurable seam
    upstream_pr: "make it configurable"
"""


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(cwd)},
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


class GuardHarness:
    """A repo with an upstream ref, a fork base, and a working branch."""

    def __init__(self, root: Path) -> None:
        self.root = root
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "fork@example.test")
        git(root, "config", "user.name", "Fork Test")
        # Upstream tree: a seam, a plain source file, a test, and a lockfile.
        for rel, body in [
            ("seam.swift", "let productionIdentifiers = [\"com.omi.app\"]\n"),
            ("backend/service.py", "VALUE = 1\n"),
            ("backend/tests/test_service.py", "def test_value():\n    assert True\n"),
            ("backend/pylock.toml", "[lock]\n"),
        ]:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "upstream baseline")
        git(root, "update-ref", "refs/remotes/upstream/main", "HEAD")
        git(root, "branch", "-f", "base", "HEAD")
        allow = root / "dev/unified-main"
        allow.mkdir(parents=True, exist_ok=True)
        (allow / "upstream-touch-allowlist.yaml").write_text(ALLOWLIST, encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "fork allowlist")
        git(root, "branch", "-f", "base", "HEAD")

    def commit(self, rel: str, body: str, message: str = "change") -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", message)

    def run(self, *extra: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, str(GUARD), "--base", "base", "--head", "HEAD",
             "--upstream-ref", "refs/remotes/upstream/main",
             "--allowlist", "dev/unified-main/upstream-touch-allowlist.yaml", "--json", *extra],
            cwd=self.root, capture_output=True, text=True,
        )
        try:
            return proc.returncode, json.loads(proc.stdout)
        except json.JSONDecodeError:  # pragma: no cover - surfaces guard crashes
            raise AssertionError(f"non-JSON output (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}")


class UpstreamTouchGuardTests(unittest.TestCase):
    def harness(self) -> GuardHarness:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return GuardHarness(Path(tmp.name))

    def test_fork_owned_new_file_is_always_clean(self):
        h = self.harness()
        h.commit("backend/fork/storage.py", "MINIO = True\n")
        rc, out = h.run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["upstream_files_changed"], 0)

    def test_unlisted_upstream_file_fails_with_a_remedy(self):
        h = self.harness()
        h.commit("backend/service.py", "VALUE = 1\nFORK = True\n")
        rc, out = h.run()
        self.assertEqual(rc, 1)
        [v] = out["violations"]
        self.assertEqual(v["kind"], "not-allowlisted")
        # The failure must say what to do instead, not only that it failed.
        self.assertIn("backend/fork/", v["remedy"])

    def test_allowlisted_seam_within_budget_passes(self):
        h = self.harness()
        h.commit("seam.swift", "let productionIdentifiers = readFromPlist()\n")
        rc, out = h.run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["violations"], [])
        self.assertTrue(any("seam.swift" in a for a in out["allowed"]))

    def test_allowlisted_seam_over_budget_fails(self):
        h = self.harness()
        h.commit("seam.swift", "".join(f"let extra{i} = {i}\n" for i in range(9)))
        rc, out = h.run()
        self.assertEqual(rc, 1)
        [v] = out["violations"]
        self.assertEqual(v["kind"], "over-budget")
        self.assertIn("budget is 3", v["detail"])

    def test_upstream_test_is_forbidden_even_if_allowlisted(self):
        h = self.harness()
        allow = h.root / "dev/unified-main/upstream-touch-allowlist.yaml"
        allow.write_text(
            ALLOWLIST + "\n  - path: backend/tests/test_service.py\n    max_added_lines: 50\n    reason: nope\n",
            encoding="utf-8",
        )
        h.commit("backend/tests/test_service.py", "def test_value():\n    assert 1 == 1\n")
        rc, out = h.run()
        self.assertEqual(rc, 1)
        self.assertEqual([v["kind"] for v in out["violations"]], ["forbidden"])

    def test_allowlist_declared_forbidden_pattern_is_honoured(self):
        h = self.harness()
        h.commit("backend/pylock.toml", "[lock]\nfork = true\n")
        rc, out = h.run()
        self.assertEqual(rc, 1)
        [v] = out["violations"]
        self.assertEqual(v["kind"], "forbidden")
        self.assertIn("requirements-fork.txt", v["detail"] + v["remedy"])

    def test_missing_upstream_ref_skips_instead_of_passing_silently(self):
        h = self.harness()
        h.commit("backend/service.py", "VALUE = 2\n")
        proc = subprocess.run(
            [sys.executable, str(GUARD), "--base", "base", "--head", "HEAD",
             "--upstream-ref", "refs/remotes/upstream/does-not-exist",
             "--allowlist", "dev/unified-main/upstream-touch-allowlist.yaml", "--json"],
            cwd=h.root, capture_output=True, text=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0)
        # ok is null, not true: an unevaluated check must not read as a pass.
        self.assertIsNone(payload["ok"])
        self.assertIn("git fetch upstream", payload["skipped"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
