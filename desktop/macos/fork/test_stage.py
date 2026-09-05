#!/usr/bin/env python3
"""Behavioral identity probe plus explicitly static compiler-owner tripwires."""

import json
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build import install_brand_package_resources, local_info_plist
from ci_build import TARGETS, build_matrix, synthetic_manifest
from prepare import AUTH_REPLACEMENTS, ROOT, stage
from swift_overlay import OverlayError, load_owners, rewrite_functions


class NativeStageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="native-stage-contract-")
        cls.directory = Path(cls.temporary.name)
        value = synthetic_manifest(cls.directory)
        value["brand"].update(id="synthetic-native", display_name="Synthetic Native")
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

    def test_selected_resource_hashes_do_not_retain_the_reviewed_upstream_rasters(self):
        for name in ("omi_app_icon.png", "omi_menu_bar_icon.png", "herologo.png"):
            self.assertNotEqual(
                self.proof["assets"]["outputs"][f"Desktop/Sources/Resources/{name}"]["sha256"],
                self.proof["source_owners"][f"Resources/{name}"],
            )

    def test_compile_matrix_selects_both_deployment_targets_with_distinct_named_bundles(self):
        output = self.directory / "matrix-output"
        calls = []

        def fake_build(manifest, target, app_name, stage, dependency_cache, *, compile_only):
            calls.append((manifest, target, app_name, stage, dependency_cache, compile_only))
            return stage / "Desktop/.build/debug/Omi Computer"

        with patch("ci_build.build", side_effect=fake_build):
            result = build_matrix(output)

        self.assertEqual(tuple(result), TARGETS)
        self.assertEqual([call[1] for call in calls], ["self_hosted", "cloudflare"])
        self.assertEqual([call[2] for call in calls], ["omi-native-ci-self-hosted", "omi-native-ci-cloudflare"])
        self.assertEqual(
            [call[3] for call in calls],
            [(output / "self_hosted").resolve(), (output / "cloudflare").resolve()],
        )
        self.assertTrue(all(call[5] for call in calls))

    def test_compile_matrix_refuses_existing_output_before_staging(self):
        output = self.directory / "existing-matrix-output"
        output.mkdir()
        with self.assertRaisesRegex(ValueError, "fresh directory"):
            build_matrix(output)

    def test_compile_matrix_removes_partial_first_target_after_second_target_failure(self):
        output = self.directory / "failed-matrix-output"

        def fail_on_cloudflare(_manifest, target, _app_name, stage, _cache, *, compile_only):
            stage.mkdir(parents=True)
            if target == "cloudflare":
                raise RuntimeError("synthetic Cloudflare compile failure")
            return stage / "Desktop/.build/debug/Omi Computer"

        with patch("ci_build.build", side_effect=fail_on_cloudflare), self.assertRaisesRegex(
            RuntimeError, "Cloudflare"
        ):
            build_matrix(output)
        self.assertFalse(output.exists())

    def test_generated_brand_images_decode_with_appkit_and_actual_sign_in_view_typechecks(self):
        good = self.directory / "Good.bundle/Contents"
        missing = self.directory / "Missing.bundle/Contents"
        corrupt = self.directory / "Corrupt.bundle/Contents"
        for root in (good, missing, corrupt):
            (root / "Resources").mkdir(parents=True)
            (root / "Info.plist").write_bytes(
                plistlib.dumps({"CFBundleIdentifier": "invalid.example.brand." + root.parent.name.lower()})
            )
        resources = self.output / "Desktop/Sources/Resources"
        for name in ("ForkBrandLight.png", "ForkBrandDark.png"):
            shutil.copy2(resources / name, good / "Resources" / name)
        (corrupt / "Resources/ForkBrandLight.png").write_bytes(b"not an image")
        probe = self.directory / "brand-probe.swift"
        probe.write_text('''import AppKit
@main struct BrandProbe {
  static func main() throws {
    let good = Bundle(path: CommandLine.arguments[1])!
    for dark in [false, true] {
      let image = try ForkNativeBrand.image(dark: dark, in: good)
      precondition(image.isValid && image.size == NSSize(width: 256, height: 256))
      precondition(image.tiffRepresentation != nil)
    }
    for (path, expected) in [(CommandLine.arguments[2], "missing"), (CommandLine.arguments[3], "invalid")] {
      do { _ = try ForkNativeBrand.image(dark: false, in: Bundle(path: path)!); fatalError("accepted \(expected)") }
      catch ForkNativeBrand.AssetError.missing { precondition(expected == "missing") }
      catch ForkNativeBrand.AssetError.invalidImage { precondition(expected == "invalid") }
      catch { fatalError("unexpected error") }
    }
    print("native brand decoding passed")
  }
}
''')
        source = self.output / "Desktop/Sources/ForkNative/ForkNativeBrand.swift"
        binary = self.directory / "brand-probe"
        subprocess.run(
            ["xcrun", "swiftc", str(source), str(probe), "-framework", "AppKit", "-o", str(binary)], check=True
        )
        result = subprocess.run(
            [str(binary), str(good.parent), str(missing.parent), str(corrupt.parent)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "native brand decoding passed")

        stubs = self.directory / "sign-in-stubs.swift"
        stubs.write_text('''import SwiftUI
enum SessionPhase { case signedOut, recoveryRequired }
@MainActor final class AuthState: ObservableObject {
  @Published var error: String?
  var isLoading = false
  var sessionPhase = SessionPhase.signedOut
}
@MainActor final class AuthService {
  static let shared = AuthService()
  func signInWithEmail(email: String, password: String, name: String?) async throws {}
  func retryRestoredSession() async {}
}
enum ForkDesktopBuild { static let productName = "Synthetic Native" }
extension Bundle { static let resourceBundle = Bundle.main }
''')
        subprocess.run(
            [
                "xcrun",
                "swiftc",
                "-swift-version",
                "6",
                "-typecheck",
                str(stubs),
                str(source),
                str(self.output / "Desktop/Sources/SignInView.swift"),
            ],
            check=True,
        )

    def test_package_boundary_rejects_stale_swiftpm_resources_and_installs_selected_icon(self):
        packaged = self.directory / "package-resources"
        bundle = packaged / "Omi Computer_Omi Computer.bundle"
        bundle.mkdir(parents=True)
        source = self.output / "Desktop/Sources/Resources"
        for name in (
            "omi_app_icon.png",
            "omi_menu_bar_icon.png",
            "herologo.png",
            "ForkBrandLight.png",
            "ForkBrandDark.png",
        ):
            shutil.copy2(source / name, bundle / name)
        installed = install_brand_package_resources(self.output, packaged, self.proof)
        self.assertEqual(
            set(installed),
            {
                f"Omi Computer_Omi Computer.bundle/{name}"
                for name in (
                    "omi_app_icon.png",
                    "omi_menu_bar_icon.png",
                    "herologo.png",
                    "ForkBrandLight.png",
                    "ForkBrandDark.png",
                )
            }
            | {"ForkAppIcon.icns"},
        )
        self.assertEqual((packaged / "ForkAppIcon.icns").read_bytes(), (self.output / "ForkAppIcon.icns").read_bytes())
        plist = plistlib.loads(
            local_info_plist(
                self.proof,
                plistlib.dumps({"SUFeedURL": "https://upstream.invalid", "SUPublicEDKey": "upstream-key"}),
            )
        )
        self.assertEqual(plist["CFBundleDisplayName"], "Synthetic Native")
        self.assertEqual(plist["CFBundleIconFile"], "ForkAppIcon")
        self.assertEqual(plist["CFBundleName"], "omi-auth-contract")
        self.assertNotIn("SUFeedURL", plist)
        self.assertNotIn("SUPublicEDKey", plist)
        (bundle / "herologo.png").write_bytes((bundle / "ForkBrandDark.png").read_bytes())
        with self.assertRaisesRegex(ValueError, "herologo"):
            install_brand_package_resources(self.output, packaged, self.proof)

    def test_invalid_manifest_asset_removes_the_partial_stage(self):
        value = synthetic_manifest(self.directory, "notebook")
        invalid = self.directory / "assets/notebook-logo_dark.png"
        invalid.write_bytes(b"not a png")
        manifest = self.directory / "invalid-brand.json"
        manifest.write_text(json.dumps(value))
        output = self.directory / "invalid-stage"
        with self.assertRaises(subprocess.CalledProcessError):
            stage(manifest, "self_hosted", "omi-invalid-assets", output)
        self.assertFalse(output.exists())

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
