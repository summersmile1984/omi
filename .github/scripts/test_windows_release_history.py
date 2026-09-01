#!/usr/bin/env python3
"""Focused contract tests for the Windows release history dry-run planner."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("windows_release_history.py")
SPEC = importlib.util.spec_from_file_location("windows_release_history", SCRIPT)
assert SPEC and SPEC.loader
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def export() -> dict:
    release_id = "v1.2.3-windows"
    version = "1.2.3"
    root = f"https://github.com/BasedHardware/omi/releases/download/{release_id}"
    return {
        "schema_version": 1,
        "source": {
            "kind": "github-release",
            "repository": "BasedHardware/omi",
            "release_id": release_id,
            "release_fingerprint": _sha("github-release-snapshot"),
        },
        "release": {
            "release_id": release_id,
            "version": version,
            "build_number": 123,
            "prerelease": True,
            "channel": "beta",
            "assets": {
                "exe": {"url": f"{root}/Omi-for-Windows-Setup-{version}.exe", "sha256": _sha("exe")},
                "blockmap": {"url": f"{root}/Omi-for-Windows-Setup-{version}.exe.blockmap", "sha256": _sha("blockmap")},
                "latest_yml": {"url": f"{root}/latest.yml", "sha256": _sha("latest")},
            },
        },
    }


class WindowsReleaseHistoryContractTests(unittest.TestCase):
    def test_valid_export_produces_deterministic_plan_independent_of_input_order(self) -> None:
        first = export()
        second = json.loads(json.dumps(first, sort_keys=True, separators=(",", ":")))
        plan_one = CONTRACT.build_dry_run_plan(first)
        plan_two = CONTRACT.build_dry_run_plan(second)
        self.assertEqual(plan_one, plan_two)
        self.assertEqual(plan_one["mode"], "dry-run")
        self.assertEqual(plan_one["status"], "planned")
        self.assertEqual(plan_one["action"], "stage")
        self.assertEqual(plan_one["plan_hash"], _sha(json.dumps({key: value for key, value in plan_one.items() if key != "plan_hash"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))

    def test_release_identity_and_channel_are_bound(self) -> None:
        for field, value in (("release_id", "v1.2.3+123-windows"), ("version", "1.2.4"), ("build_number", 0)):
            candidate = export()
            candidate["release"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(CONTRACT.WindowsReleaseHistoryError, "release"):
                CONTRACT.build_dry_run_plan(candidate)

        candidate = export()
        candidate["release"]["channel"] = "stable"
        with self.assertRaisesRegex(CONTRACT.WindowsReleaseHistoryError, "channel"):
            CONTRACT.build_dry_run_plan(candidate)

    def test_rejects_secrets_and_path_traversal_or_untrusted_github_urls(self) -> None:
        secret = export()
        secret["source"]["operator_token"] = "ghp_not-allowed"
        with self.assertRaisesRegex(CONTRACT.WindowsReleaseHistoryError, "credential"):
            CONTRACT.build_dry_run_plan(secret)

        for url in (
            "https://github.com/BasedHardware/omi/releases/download/v1.2.3-windows/../latest.yml",
            "https://github.com/BasedHardware/omi/releases/download/v1.2.3-windows/Omi-for-Windows-Setup-1.2.3.exe?token=secret",
            "https://github.com:bad/BasedHardware/omi/releases/download/v1.2.3-windows/latest.yml",
            "https://github.com/attacker/omi/releases/download/v1.2.3-windows/latest.yml",
            "https://user:pass@github.com/BasedHardware/omi/releases/download/v1.2.3-windows/latest.yml",
            "https://github.com/BasedHardware/omi/releases/download/v1.2.3-windows/%6catest.yml",
        ):
            candidate = deepcopy(export())
            candidate["release"]["assets"]["latest_yml"]["url"] = url
            with self.subTest(url=url), self.assertRaises(CONTRACT.WindowsReleaseHistoryError):
                CONTRACT.build_dry_run_plan(candidate)

    def test_cli_bounds_input_and_does_not_include_local_paths_or_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            export_path = Path(directory) / "export.json"
            output_path = Path(directory) / "plan.json"
            export_path.write_text(json.dumps(export()), encoding="utf-8")
            self.assertEqual(CONTRACT.main(["--export", str(export_path), "--output", str(output_path)]), 0)
            output = output_path.read_text(encoding="utf-8")
            self.assertNotIn(directory, output)
            self.assertNotIn("secret", output.lower())
            self.assertEqual(json.loads(output), CONTRACT.build_dry_run_plan(export()))

            export_path.write_bytes(b"{" + b"x" * CONTRACT.MAX_EXPORT_BYTES)
            self.assertEqual(CONTRACT.main(["--export", str(export_path)]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
