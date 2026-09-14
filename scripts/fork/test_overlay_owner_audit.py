#!/usr/bin/env python3
"""Prove the overlay-owner audit detects every drift shape and actually runs for it.

The audit exists because one upstream import invalidated many fork source owners at once,
and each build stage stops at the first mismatch. It is only worth having if both halves
hold: it covers every registry the fork verifies, and the manifest selects it for a diff
that can invalidate an owner it covers.

Both halves failed together for the Electron registry. desktop/windows/package.json drifted
(upstream #12753 replaced the `dev` script with a wrapper) and `fork-electron-native-identity`
was the check that caught it in CI — the audit never looked at desktop/windows/fork/
source-owners.json, and its triggers stopped at desktop/macos/**.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / "scripts/fork/check-overlay-owners.py"
MANIFEST = ROOT / ".github/checks-manifest.fork.yaml"
AUDIT_ID = "fork-overlay-owner-audit"
REGISTRIES = (
    "app/fork/source-owners.json",
    "desktop/macos/fork/source-owners.json",
    "desktop/windows/fork/source-owners.json",
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # run_checks.py declares a dataclass, which resolves its own module at class creation.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ElectronOwnerAuditTests(unittest.TestCase):
    """The Electron audit against disposable components, not the live repository."""

    def setUp(self) -> None:
        self.audit = load_module("overlay_owner_audit", AUDIT)
        self.temporary = tempfile.TemporaryDirectory(prefix="overlay-owner-audit-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.component = self.base / "windows"
        (self.component / "src").mkdir(parents=True)
        self.registry = self.base / "source-owners.json"

    def write(self, relative: str, text: str) -> str:
        path = self.component / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def audit_with(self, owners: dict[str, str]):
        self.registry.write_text(json.dumps(owners), encoding="utf-8")
        return self.audit.audit_electron(registry_path=self.registry, component=self.component)

    def test_matching_owners_are_clean(self):
        digest = self.write("src/main/index.ts", "export const identity = 'fork'\n")
        self.assertEqual(self.audit_with({"src/main/index.ts": digest}), ([], [], 1))

    def test_changed_owner_is_reported_stale(self):
        self.write("src/main/index.ts", "export const identity = 'fork'\n")
        stale, unresolved, _ = self.audit_with({"src/main/index.ts": "0" * 64})
        self.assertEqual((stale, unresolved), (["src/main/index.ts"], []))

    def test_missing_owner_is_unresolved_rather_than_clean(self):
        stale, unresolved, _ = self.audit_with({"src/main/index.ts": hashlib.sha256(b"gone").hexdigest()})
        self.assertEqual(stale, [])
        self.assertEqual(unresolved, ["src/main/index.ts (missing)"])

    def test_owner_escaping_the_component_is_refused(self):
        outside = self.base / "outside.json"
        outside.write_text("upstream\n", encoding="utf-8")
        stale, unresolved, _ = self.audit_with({"../outside.json": hashlib.sha256(outside.read_bytes()).hexdigest()})
        self.assertEqual(stale, [])
        self.assertEqual(unresolved, ["../outside.json (escapes the component root)"])

    def test_every_stale_owner_is_listed_not_only_the_first(self):
        # prepare.py raises on the first mismatch, so one owner per push costs one CI run each.
        owners = {"src/a.ts": self.write("src/a.ts", "a\n"), "src/b.ts": "0" * 64, "src/c.ts": "1" * 64}
        self.write("src/b.ts", "b\n")
        self.write("src/c.ts", "c\n")
        stale, unresolved, total = self.audit_with(owners)
        self.assertEqual(stale, ["src/b.ts", "src/c.ts"])
        self.assertEqual(unresolved, [])
        self.assertEqual(total, 3)


class ManifestWiringTests(unittest.TestCase):
    """The audit only helps if the manifest selects it for the diffs that can stale an owner."""

    @classmethod
    def setUpClass(cls) -> None:
        # Load the manifest through the runner's own stdlib subset reader rather than
        # PyYAML: the test then needs no site packages, and it proves what the runner reads.
        sys.path.insert(0, str(ROOT / ".github/scripts"))
        cls.run_checks = load_module("run_checks", ROOT / ".github/scripts/run_checks.py")
        manifest = cls.run_checks.load_manifest(MANIFEST)
        cls.check = next(check for check in manifest.checks if check.id == AUDIT_ID)

    def test_every_registry_the_fork_verifies_is_audited(self) -> None:
        command = " ".join(self.check.command)
        self.assertIn("check-overlay-owners.py", command)
        self.assertIn("test_overlay_owner_audit.py", command)
        for registry in REGISTRIES:
            self.assertTrue((ROOT / registry).is_file(), f"{registry} is verified by a build stage")

    def test_triggers_fire_on_the_diff_that_reddened_the_gate(self) -> None:
        paths = (
            "desktop/windows/package.json",
            "desktop/windows/src/main/index.ts",
            "app/lib/main.dart",
            "desktop/macos/Desktop/Sources/AppBuild.swift",
            # An audit nobody re-runs after it is edited is the same dead check.
            "scripts/fork/check-overlay-owners.py",
            "scripts/fork/test_overlay_owner_audit.py",
        )
        for path in paths:
            self.assertTrue(
                any(self.run_checks.trigger_matches(trigger, path) for trigger in self.check.triggers),
                f"{path} can stale an audited owner but selects no audit",
            )


if __name__ == "__main__":
    unittest.main()
