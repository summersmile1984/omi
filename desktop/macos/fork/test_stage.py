#!/usr/bin/env python3
"""Behavioral identity probe plus explicitly static compiler-owner tripwires."""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from prepare import AUTH_REPLACEMENTS, ROOT, load_manifest, stage
from swift_overlay import OverlayError, load_owners, rewrite_functions


class NativeStageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="native-stage-contract-")
        cls.directory = Path(cls.temporary.name)
        value = copy.deepcopy(load_manifest("omi-upstream", ROOT))
        value["brand"].update(id="synthetic-native", display_name="Synthetic Native")
        value["identifiers"].update(
            macos_bundle_id="test.synthetic.native",
            macos_bundle_id_beta="test.synthetic.native.beta",
            macos_bundle_id_dev="test.synthetic.native.dev",
            macos_named_bundle_prefix="test.synthetic.",
            keychain_service_prefix="test.synthetic.",
        )
        value["deployments"] = {
            "cloudflare": {
                "local": {
                    "api_base": "http://127.0.0.1:33062",
                    "auth_base": "http://127.0.0.1:33058",
                    "web_app": "http://127.0.0.1:33059",
                    "mcp_base": "http://127.0.0.1:33062/mcp",
                    "share_base": "http://127.0.0.1:33062/share",
                    "objects_base": "http://127.0.0.1:33062/objects",
                }
            }
        }
        cls.manifest = cls.directory / "brand.json"
        cls.manifest.write_text(json.dumps(value))
        cls.output = cls.directory / "stage"
        cls.proof = stage(cls.manifest, "cloudflare", "omi-auth-contract", cls.output)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_real_staged_identity_functions_isolate_brand_storage_and_disable_updater(self):
        main = self.directory / "main.swift"
        main.write_text('''import Foundation
let bundle = "test.synthetic.omi-auth-contract"
let build = AppBuild.configuration(bundleIdentifier: bundle, infoDictionary: [:])
precondition(build.isNonProduction)
precondition(build.isNamedDevelopmentBundle)
precondition(build.allowsLocalAutomation)
precondition(!build.allowsSparkleUpdates)
precondition(!AppBuild.mayRunLegacyStableAppCleanup(bundleIdentifier: bundle))
let storage = DesktopStorageIdentity(bundleIdentifier: bundle, localProfileEnabled: false, localProfileStorageName: nil)
precondition(storage.isNamedDevelopmentBundle)
precondition(storage.usesIsolatedStorage)
precondition(storage.applicationSupportPathComponents == ["synthetic-native Dev Bundles", bundle])
precondition(!DesktopLocalProfile.isEnabled(bundleIdentifier: bundle, profileValue: nil))
print("native identity behavior passed")
''')
        source = self.output / "Desktop/Sources"
        binary = self.directory / "identity-probe"
        subprocess.run(
            [
                "xcrun",
                "swiftc",
                "-package-name",
                "ForkStage",
                str(source / "AppBuild.swift"),
                str(source / "OmiSupport/DesktopLocalProfile.swift"),
                str(main),
                "-o",
                str(binary),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "native identity behavior passed")

    def test_source_and_existing_output_are_never_mutated(self):
        with self.assertRaises(ValueError):
            stage(self.manifest, "cloudflare", "omi-auth-contract", self.output)
        with self.assertRaises(ValueError):
            stage(self.manifest, "cloudflare", "omi-auth-contract", ROOT / "native-stage-output")
        self.assertFalse((ROOT / "native-stage-output").exists())

    def test_published_or_non_named_artifacts_are_not_admitted_by_local_package(self):
        for target, name in [("omi_cloud", "omi-auth-contract"), ("cloudflare", "Omi"), ("cloudflare", "../Omi")]:
            with self.assertRaises(ValueError):
                stage(self.manifest, target, name, self.directory / "rejected")

    def test_static_compiler_owner_tripwire_rejects_an_unreviewed_auth_change(self):
        # omi-test-quality: source-inspection -- static staging owner contract;
        # real runtime identity and auth behavior execute in the other tests.
        source = ROOT / "desktop/macos/Desktop/Sources/AuthService.swift"
        changed = self.directory / "AuthService.swift"
        changed.write_text(
            source.read_text().replace(
                "guard !isConfigured else { return }", "guard !isConfigured else { return } // changed owner"
            )
        )
        with self.assertRaises(OverlayError):
            rewrite_functions(changed, {"configure()": AUTH_REPLACEMENTS["configure()"]}, load_owners())


if __name__ == "__main__":
    unittest.main()
