"""Regression paths from the 2026-09-04 main audit, E1/E2/E3.

Run the actual CLI in isolated repositories. No production manifest is edited.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_brand_tooling import RepoFixture


class BrandBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fx = RepoFixture(Path(self.tmp.name))
        self.brand = "a-real-fork-brand"
        self.output = self.fx.root / "app/lib/flavors.brand.dart"

    def test_check_is_read_only_for_dirty_missing_and_untracked_outputs(self):
        self.assertEqual(self.fx.run_apply(self.brand).returncode, 0)
        subprocess.run(["git", "add", "."], cwd=self.fx.root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture Test",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "baseline",
            ],
            cwd=self.fx.root,
            check=True,
        )
        for content in (b"CORRUPTED\n", None):
            with self.subTest(content=content):
                if content is None:
                    self.output.unlink()
                else:
                    self.output.write_bytes(content)
                proc = self.fx.run_apply(self.brand, "--check-clean", "--json")
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertEqual(json.loads(proc.stdout)["drift"], ["app/lib/flavors.brand.dart"])
                self.assertEqual(self.output.read_bytes() if self.output.exists() else None, content)
        subprocess.run(
            ["git", "rm", "--cached", "-f", "app/lib/flavors.brand.dart"],
            cwd=self.fx.root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        self.output.write_bytes(b"untracked stale")
        self.assertEqual(self.fx.run_apply(self.brand, "--check-clean").returncode, 1)
        self.assertEqual(self.output.read_bytes(), b"untracked stale")

    def test_check_matches_bytes_even_without_git(self):
        import shutil

        shutil.rmtree(self.fx.root / ".git")
        self.assertEqual(self.fx.run_apply(self.brand).returncode, 0)
        before = self.output.stat().st_mtime_ns
        self.assertEqual(self.fx.run_apply(self.brand, "--check-clean").returncode, 0)
        self.assertEqual(self.output.stat().st_mtime_ns, before)

    def test_release_rejects_skipped_and_title_only_categories_before_writes(self):
        for args in (("--release",), ("--release", "--only", "flutter"), ("--release", "--only", "desktop")):
            with self.subTest(args=args):
                proc = self.fx.run_apply(self.brand, *args, "--json")
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                report = json.loads(proc.stdout)
                self.assertFalse(report["release_ready"])
                self.assertTrue(report["skipped"] or report["partial"])
                self.assertFalse(self.output.exists())

    def test_missing_manifest_and_stale_generation_cannot_lower_baseline(self):
        baseline = self.fx.root / "baseline.txt"
        missing = self.fx.run_check("missing", "--baseline", str(baseline))
        self.assertEqual(missing.returncode, 1)
        self.assertFalse(baseline.exists())
        baseline.write_text("100\n")
        stale = self.fx.run_check(self.brand, "--baseline", str(baseline))
        self.assertEqual(stale.returncode, 1)
        self.assertEqual(baseline.read_text(), "100\n")
        self.assertFalse(self.output.exists())

    def test_overlay_manifest_id_and_output_root_are_used_by_both_clis(self):
        path = self.fx.root / "private.json"
        data = json.loads((self.fx.root / "brand/a-real-fork-brand/manifest.yaml").read_text())
        data["brand"].update(id="private-weft", display_name="Private Weft")
        path.write_text(json.dumps(data))
        output = self.fx.root / "build-overlay"
        args = ("--manifest", str(path), "--output-root", str(output))
        applied = self.fx.run_apply("private-weft", *args)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn("Private Weft", (output / "app/lib/flavors.brand.dart").read_text())
        checked = self.fx.run_check("private-weft", *args)
        self.assertTrue(json.loads(checked.stdout)["apply_check_clean"])
        mismatch = self.fx.run_apply(self.brand, *args)
        self.assertEqual(mismatch.returncode, 1)
        self.assertFalse(self.output.exists())

    def test_web_tsx_text_attributes_literals_and_escapes_are_scanned(self):
        self.assertEqual(self.fx.run_apply(self.brand).returncode, 0)
        web = self.fx.root / "web/app/src/Page.tsx"
        web.parent.mkdir(parents=True)
        web.write_text(
            'export const Page = () => <h1 aria-label="Omi">Omi</h1>;\n'
            'const url = "https://api.omi.me";\n'
            'const escaped = "Om\\u0069";\n'
            '// Omi in a source comment is not an exposed string.\n'
        )
        windows = self.fx.root / "desktop/windows/src/Page.tsx"
        windows.parent.mkdir(parents=True)
        windows.write_text('export const Page = () => <h1>Omi</h1>;\n')
        report = json.loads(self.fx.run_check(self.brand).stdout)
        web_hits = [m for m in report["matches"] if m["surface"] == "web_source"]
        self.assertTrue(any(m["word"] == "omi.me" for m in web_hits))
        self.assertEqual(sum(m["word"] == "Omi" for m in web_hits), 3)
        self.assertFalse(any(m["line"] == 5 for m in web_hits))
        self.assertTrue(any(m["surface"] == "windows_source" for m in report["matches"]))

    def test_export_scopes_are_explicit_and_never_claim_binary_inspection(self):
        self.fx.run_apply(self.brand)
        build = self.fx.root / "artifacts"
        runtime = self.fx.root / "exports"
        build.mkdir()
        runtime.mkdir()
        (build / "metadata.json").write_text('{"display_name":"Omi"}')
        (build / "icon.png").write_bytes(b"binary")
        (runtime / "prompt.txt").write_text("You are Omi.")
        report = json.loads(
            self.fx.run_check(self.brand, "--build-root", str(build), "--runtime-root", str(runtime)).stdout
        )
        self.assertEqual(report["exports"]["build"]["unscanned"], ["icon.png"])
        self.assertTrue(any(m["surface"] == "runtime" for m in report["matches"]))
        self.assertTrue(any(m["surface"] == "build" for m in report["matches"]))
        self.assertEqual(self.fx.run_check(self.brand, "--release").returncode, 1)
        self.assertEqual(
            self.fx.run_check(
                self.brand, "--release", "--build-root", str(build), "--runtime-root", str(runtime)
            ).returncode,
            1,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
