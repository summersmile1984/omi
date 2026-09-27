#!/usr/bin/env python3
"""Test the WHITELABEL-MODEL six-seam guard.

Exercises the path-matching rules in check_whitelabel_seam.py against the
declared seams, the endorsed fork-owned paths, the four apply/shim/stage
patterns, and a representative upstream path. The seam guard is a
load-bearing contract: a regression here lets a fork-owned edit slip
through into the next upstream sync.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check_whitelabel_seam.py"
spec = importlib.util.spec_from_file_location("check_whitelabel_seam", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DeclaredSeamsTest(unittest.TestCase):
    def test_all_six_declared_seams_are_endorsed(self):
        for path in module.DECLARED_SEAMS:
            with self.subTest(path=path):
                self.assertTrue(module.is_endorsed(path), f"declared seam must be endorsed: {path}")

    def test_seam_count_matches_whitelabel_model(self):
        # WHITELABEL-MODEL.md §2 lists exactly six declared seams. If this
        # number changes, the model doc and the seam guard must move
        # together. Counting the seam set catches a silent drift.
        self.assertEqual(len(module.DECLARED_SEAMS), 6)


class BrandGlobTest(unittest.TestCase):
    def test_brand_glob_catches_known_fork_owned_paths(self):
        for path in (
            "brand/eddy/manifest.yaml",
            "brand/_schema/manifest.schema.json",
            "scripts/brand/apply.py",
            "scripts/brand/generators/mobile.py",
            "scripts/profiles/render.py",
            "deploy/profiles/cloudflare.yaml",
            "backend/fork/brand_transport.py",
            "backend/fork/patches/auth.py",
            "app/fork/prepare.py",
            "app/fork/identity/runtime.dart",
            "desktop/windows/fork/brand-stage.mjs",
            "omi/firmware/fork/stage.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(module.is_brand_path(path))

    def test_brand_glob_ignores_unrelated_paths(self):
        for path in (
            "app/lib/utils/device.dart",
            "app/lib/backend/http/shared.dart",
            "desktop/macos/Desktop/Sources/AppBuild.swift",
            "backend/utils/llm/chat.py",
            "docs/product/invariants/brand-ui.md",
        ):
            with self.subTest(path=path):
                self.assertFalse(
                    module.is_brand_path(path),
                    f"upstream source path must not be classified as brand-glob: {path}",
                )


class EndorsedPathTest(unittest.TestCase):
    def test_endorsed_paths_cover_all_four_patterns(self):
        # Pattern A: frontend prepare
        self.assertTrue(module.is_endorsed("app/fork/prepare.py"))
        self.assertTrue(module.is_endorsed("desktop/windows/fork/prepare.py"))
        self.assertTrue(module.is_endorsed("desktop/macos/fork/prepare.py"))
        # Pattern B: backend shim
        self.assertTrue(module.is_endorsed("backend/fork/patches/auth.py"))
        self.assertTrue(module.is_endorsed("backend/fork/brand_transport.py"))
        # Pattern C: firmware stage
        self.assertTrue(module.is_endorsed("omi/firmware/fork/stage.py"))
        # Pattern D: profile render + Cloudflare overlay
        self.assertTrue(module.is_endorsed("scripts/profiles/render.py"))
        self.assertTrue(module.is_endorsed("scripts/brand/generators/firmware.py"))
        self.assertTrue(module.is_endorsed("web/app/fork/realtime-overlay.ts"))
        self.assertTrue(module.is_endorsed("deploy/web/Dockerfile"))
        # Brand manifest + raster fixture pattern
        self.assertTrue(module.is_endorsed("brand/eddy/manifest.yaml"))
        self.assertTrue(module.is_endorsed("brand/eddy/assets/icon-master.svg"))
        # Meta (model doc, CI registration, the guard itself)
        self.assertTrue(module.is_endorsed("dev/unified-main/WHITELABEL-MODEL.md"))
        self.assertTrue(module.is_endorsed(".github/scripts/check_whitelabel_seam.py"))
        self.assertTrue(module.is_endorsed(".github/checks-manifest.fork.yaml"))

    def test_upstream_source_path_under_brand_glob_is_not_endorsed(self):
        # Even if a brand-glob prefix matched, an upstream-owned file is not
        # endorsed. The fork constitution forbids editing upstream source;
        # the seam guard catches it before the upstream-touch ratchet does.
        self.assertFalse(module.is_endorsed("backend/utils/llm/chat.py"))
        self.assertFalse(module.is_endorsed("app/lib/env/env.dart"))
        self.assertFalse(module.is_endorsed("desktop/macos/Desktop/Sources/AppBuild.swift"))


if __name__ == "__main__":
    unittest.main()
