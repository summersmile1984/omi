#!/usr/bin/env python3
"""Execute the actual fork workflow shell against disposable Git histories."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
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

    def test_manual_feature_branch_uses_all_commits_since_main(self) -> None:
        main = self.git("rev-parse", "main")
        self.git("switch", "-q", "-c", "codex/feature")
        self.commit("first feature commit")
        self.commit("second feature commit")
        self.assertEqual(self.resolve("workflow_dispatch"), f"ref={main}")

    def test_new_feature_branch_push_uses_all_commits_since_main(self) -> None:
        main = self.git("rev-parse", "main")
        self.git("switch", "-q", "-c", "codex/feature")
        self.commit("first feature commit")
        self.commit("second feature commit")
        self.assertEqual(self.resolve("push", before="0" * 40), f"ref={main}")

    def test_new_branch_zero_before_has_a_real_base(self) -> None:
        self.assertEqual(self.resolve("push", before="0" * 40), f"ref={self.base}")

    def test_pull_request_fetches_its_base_branch(self) -> None:
        self.assertEqual(self.resolve("pull_request", base="fixture-base"), f"ref={self.base}")

    def test_unavailable_push_commit_fails_before_emitting_a_base(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            self.resolve("push", before="f" * 40)
        self.assertFalse((self.root / "github-output").exists())

    def test_fork_push_hook_preserves_checks_without_leaking_git_context(self) -> None:
        scripts = self.root / 'scripts'
        scripts.mkdir()
        downstream = scripts / 'pre-push-singleflight'
        checker = ROOT / '.github/scripts/check_agents_md_lean.py'
        # Exercise the exact upstream self-test that failed in the real linked
        # worktree push. Only the rest of the long push suite is substituted.
        program = 'import runpy,sys; runpy.run_path(sys.argv[1])["self_test"]()'
        downstream.write_text(
            '#!/bin/sh\nexec ' + ' '.join(map(shlex.quote, [sys.executable, '-c', program, str(checker)])) + '\n'
        )
        downstream.chmod(0o755)
        result = subprocess.run(
            [
                'git',
                '-c',
                f'core.hooksPath={ROOT / "scripts/fork/git-hooks"}',
                'push',
                'origin',
                'HEAD:refs/heads/verified',
            ],
            cwd=self.root,
            env={
                **os.environ,
                'GIT_DIR': str(self.root / '.git'),
                'GIT_WORK_TREE': str(self.root),
                'GIT_INDEX_FILE': str(self.root / '.git/index'),
            },
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git('rev-parse', 'verified'), self.git('rev-parse', 'HEAD'))


class ManifestTests(unittest.TestCase):
    def test_both_jobs_bootstrap_python_in_runner_account(self) -> None:
        workflow = yaml.safe_load(WORKFLOW.read_text())
        for name, job in workflow['jobs'].items():
            command = next(step['run'] for step in job['steps'] if step.get('name') == 'Provision CI Python')
            with tempfile.TemporaryDirectory(prefix='fork-ci-python-') as directory:
                root = Path(directory)
                managed_bin = root / 'managed python/bin'
                managed_bin.mkdir(parents=True)
                uv = root / 'uv'
                uv.write_text(
                    '#!/bin/sh\n'
                    'if [ "$*" = "python install 3.12" ]; then exit "$FIXTURE_INSTALL_STATUS"; fi\n'
                    '[ "$*" = "python find --managed-python 3.12" ] || exit 64\n'
                    'printf "%s/python3.12\\n" "$FIXTURE_MANAGED_BIN"\n'
                )
                uv.chmod(0o755)
                output = root / 'github-path'
                for status in (0, 1):
                    with self.subTest(job=name, install_status=status):
                        output.unlink(missing_ok=True)
                        result = subprocess.run(
                            ['bash', '-euo', 'pipefail', '-c', command],
                            env={
                                **os.environ,
                                'PATH': f'{root}:{os.environ["PATH"]}',
                                'GITHUB_PATH': str(output),
                                'FIXTURE_MANAGED_BIN': str(managed_bin),
                                'FIXTURE_INSTALL_STATUS': str(status),
                            },
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(result.returncode, status, result.stderr)
                        self.assertEqual(output.exists(), status == 0)
                        if status == 0:
                            self.assertEqual(output.read_text().strip(), str(managed_bin))

    def test_backend_provision_restores_fork_layer_only_after_successful_sync(self) -> None:
        # make setup-backend removes packages outside the upstream lock. The
        # actual workflow must restore the hash-pinned fork layer afterwards.
        workflow = yaml.safe_load(WORKFLOW.read_text())
        command = next(
            step['run']
            for step in workflow['jobs']['fork-gate']['steps']
            if step.get('name') == 'Provision backend environment'
        )
        with tempfile.TemporaryDirectory(prefix='fork-ci-install-') as directory:
            root = Path(directory)
            marker = root / 'fork-layer'
            (root / 'make').write_text(
                '#!/bin/sh\n[ "$1" = setup-backend ] || exit 64\n' 'rm -f fork-layer\nexit "$FIXTURE_UPSTREAM_STATUS"\n'
            )
            (root / 'uv').write_text(
                f'#!{sys.executable}\nimport pathlib, sys\n'
                'args = sys.argv[1:]\n'
                'assert args[:2] == ["pip", "install"]\n'
                'assert "--no-deps" in args and "--require-hashes" in args\n'
                'assert args[args.index("--python") + 1] == "backend/.venv/bin/python"\n'
                'assert args[args.index("-r") + 1] == "backend/requirements-fork.txt"\n'
                'pathlib.Path("fork-layer").write_text("installed")\n'
            )
            for name in ('make', 'uv'):
                (root / name).chmod(0o755)
            for status in (0, 1):
                with self.subTest(upstream_status=status):
                    marker.write_text('old install')
                    (root / 'github-path').unlink(missing_ok=True)
                    result = subprocess.run(
                        ['bash', '-euo', 'pipefail', '-c', command],
                        cwd=root,
                        env={
                            **os.environ,
                            'PATH': f'{root}:{os.environ["PATH"]}',
                            'FIXTURE_UPSTREAM_STATUS': str(status),
                            'GITHUB_WORKSPACE': str(root),
                            'GITHUB_PATH': str(root / 'github-path'),
                        },
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertEqual(marker.exists(), status == 0)
                    if status == 0:
                        self.assertEqual(marker.read_text(), 'installed')
                        self.assertEqual((root / 'github-path').read_text().strip(), str(root / 'backend/.venv/bin'))
                    else:
                        self.assertFalse((root / 'github-path').exists())

    def test_actual_fork_manifest_resolves_after_backend_test_moves(self) -> None:
        # 6d9b046eec moved storage/queue tests into backend/fork/tests while the
        # manifest kept their old path. Execute the real CI selector/validator.
        subprocess.run(
            [
                sys.executable,
                str(ROOT / ".github/scripts/run_checks.py"),
                "--manifest",
                ".github/checks-manifest.fork.yaml",
                "--lane",
                "ci",
                "--base",
                "HEAD",
                "--output",
                "json",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
