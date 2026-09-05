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
from prepare import AUTH_REPLACEMENTS, ROOT, application_identity, stage
from swift_overlay import OverlayError, load_owners, rewrite_functions
from render import ProfileError
from release import command, dependencies, normalize_resource_bundle, runtime_code, signing_details, vendor_dependencies
from swift_overlay import declarations


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
        source = self.output / "Desktop/Sources"
        app = source / "OmiApp.swift"
        left, right = declarations(app)["windowTitle(displayName:version:launchMode:isNonProduction:)"][0]
        title_probe = self.directory / "title.swift"
        title_probe.write_text(
            "enum LaunchMode { case normal, rewind }\nenum TitleOwner {\n"
            + app.read_bytes()[left:right].decode()
            + "\n}\n"
        )
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
for development in [false, true] {
  precondition(TitleOwner.windowTitle(displayName: "Eddy", version: "0.1.0", launchMode: .normal, isNonProduction: development) == "Eddy v0.1.0")
  precondition(TitleOwner.windowTitle(displayName: "Eddy", version: "", launchMode: .rewind, isNonProduction: development) == "Eddy Rewind")
}
for (identity, root) in [(AppBuild.productionBundleIdentifier, "synthetic-native"), (AppBuild.betaProductionBundleIdentifier, "synthetic-native Beta")] {
  let release = AppBuild.configuration(bundleIdentifier: identity, infoDictionary: [:])
  precondition(!release.isNonProduction && !release.allowsLocalAutomation && !release.allowsSparkleUpdates)
  precondition(!AppBuild.mayRunLegacyStableAppCleanup(bundleIdentifier: identity))
  let storage = DesktopStorageIdentity(bundleIdentifier: identity, localProfileEnabled: false, localProfileStorageName: nil)
  precondition(storage.usesIsolatedStorage && storage.applicationSupportPathComponents == [root])
  precondition(!DesktopLocalProfile.isEnabled(bundleIdentifier: identity, profileValue: "1"))
}
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
                str(title_probe),
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
            shutil.copy2(resources / name, good.parent / name)
        # Execute the actual release layout conversion; moving Node alone
        # created Contents/Resources and made AppKit stop finding flat images.
        (good.parent / "node").write_bytes(b"synthetic-node-layout-entry")
        normalize_resource_bundle(good.parent)
        self.assertTrue((good / "Resources/node").is_file())
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
enum ForkDesktopBuild {
  static let productName = "Synthetic Native"
  static let tagline = "A synthetic companion"
}
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

    def test_distribution_identity_requires_matching_stage_and_brand_without_upstream_collision(self):
        value = json.loads(self.manifest.read_text())
        name = value["identifiers"]["macos_binary_name"]
        self.assertEqual(application_identity(value, name, "production", "production"), "test.synthetic.native")
        self.assertEqual(application_identity(value, name + " Beta", "beta", "beta"), "test.synthetic.native.beta")
        self.assertEqual(
            application_identity(value, "omi-production-proof", "production", "development"),
            "test.synthetic.omi-production-proof",
        )
        for app, selected_stage, distribution in [
            (name, "local", "production"),
            (name, "production", "beta"),
            ("../Eddy", "production", "production"),
        ]:
            with self.assertRaises(ValueError):
                application_identity(value, app, selected_stage, distribution)
        value["identifiers"]["macos_bundle_id"] = "com.omi.computer-macos"
        with self.assertRaisesRegex(ValueError, "upstream"):
            application_identity(value, name, "production", "production")

    def test_vendored_actual_dylib_runs_after_the_original_build_dependency_is_removed(self):
        root = self.directory / "dependency-contract"
        (root / "source").mkdir(parents=True)
        app = root / "Synthetic.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        (app / "Contents/Frameworks").mkdir()
        library = root / "source/libfixture.dylib"
        base = root / "source/libfixture-base.1.dylib"
        alias = root / "source/libfixture-base.dylib"
        base_source = root / "source/base.c"
        base_source.write_text("int base(void) { return 42; }\n")
        command(
            "xcrun",
            "clang",
            "-dynamiclib",
            str(base_source),
            "-Wl,-install_name,@rpath/libfixture-base.dylib",
            "-o",
            str(base),
        )
        alias.symlink_to(base.name)
        source = root / "source/library.c"
        source.write_text("extern int base(void); int fixture(void) { return base(); }\n")
        command("xcrun", "clang", "-dynamiclib", str(source), str(alias), "-o", str(library))
        main = root / "main.c"
        main.write_text("extern int fixture(void); int main(void) { return fixture() == 42 ? 0 : 1; }\n")
        binary = app / "Contents/MacOS/Synthetic"
        command(
            "xcrun", "clang", str(main), str(library), "-Wl,-rpath,@executable_path/../Frameworks", "-o", str(binary)
        )
        observed = vendor_dependencies(app)
        self.assertEqual(set(observed), {"libfixture.dylib", "libfixture-base.1.dylib"})
        self.assertIn("@rpath/libfixture.dylib", dependencies(binary))
        library.unlink()
        alias.unlink()
        base.unlink()
        command("codesign", "--force", "--sign", "-", str(app / "Contents/Frameworks/libfixture.dylib"))
        command("codesign", "--force", "--sign", "-", str(app / "Contents/Frameworks/libfixture-base.1.dylib"))
        command("codesign", "--force", "--sign", "-", str(binary))
        command(str(binary))

    def test_distribution_verifier_rejects_wrong_team_ad_hoc_and_unsigned_runtime(self):
        valid = "Identifier=test.synthetic.native\nAuthority=Developer ID Application: Synthetic (ABCDE12345)\nTeamIdentifier=ABCDE12345\nCodeDirectory flags=0x10000(runtime)\nTimestamp=Sep 5, 2026\n"
        signing_details(valid, "ABCDE12345", "test.synthetic.native")
        for actual in [
            valid.replace("ABCDE12345", "WRONG12345"),
            valid.replace("Developer ID Application", "Apple Development"),
            valid.replace("(runtime)", ""),
            valid.replace("Timestamp=Sep 5, 2026\n", ""),
            valid.replace("Identifier=test.synthetic.native", "Identifier=com.omi.computer-macos"),
        ]:
            with self.assertRaises(ValueError):
                signing_details(actual, "ABCDE12345", "test.synthetic.native")

    def test_universal_static_archive_is_not_packaged_or_signed_as_a_dynamic_framework(self):
        root = self.directory / "static-framework"
        root.mkdir()
        source = root / "value.c"
        source.write_text("int value(void) { return 7; }\n")
        archives = []
        for arch in ("arm64", "x86_64"):
            obj, archive = root / (arch + ".o"), root / (arch + ".a")
            command("xcrun", "clang", "-arch", arch, "-c", str(source), "-o", str(obj))
            command("xcrun", "libtool", "-static", "-o", str(archive), str(obj))
            archives.append(str(archive))
        universal = root / "SyntheticFramework"
        command("xcrun", "lipo", "-create", *archives, "-output", str(universal))
        self.assertFalse(runtime_code(universal))

    def test_real_staged_analytics_initializer_never_enrolls_in_the_upstream_project(self):
        # Execute the compiler-located production method with SDK setup as the
        # controllable network seam; this is not a source-string assertion.
        path = self.output / "Desktop/Sources/PostHogManager.swift"
        left, right = declarations(path)["initialize()"][0]
        method = path.read_bytes()[left:right].decode()
        source = self.directory / "analytics-probe.swift"
        source.write_text(
            '''import Foundation
var enrollments = 0
class PostHogConfig {
  var captureApplicationLifecycleEvents = true
  var captureScreenViews = false
  var preloadFeatureFlags = false
  init(projectToken: String, host: String) {}
}
class PostHogSDK {
  static let shared = PostHogSDK()
  func setup(_ config: PostHogConfig) { enrollments += 1 }
  func register(_ value: [String: String]) {}
}
enum AppBuild { static let currentUpdateChannel = "production" }
func log(_ message: String) {}
class PostHogManager {
  var isInitialized = false
  let apiKey = "synthetic-upstream-project"
  let host = "https://upstream.synthetic.invalid"
'''
            + method
            + '''
}
@main struct Probe {
  static func main() {
    let owner = PostHogManager()
    owner.initialize()
    owner.initialize()
    precondition(!owner.isInitialized && enrollments == 0)
  }
}
'''
        )
        binary = self.directory / "analytics-probe"
        command("xcrun", "swiftc", "-parse-as-library", str(source), "-o", str(binary))
        command(str(binary))

    def test_production_stage_embeds_https_profile_and_ad_hoc_packager_rejects_it(self):
        from build import package_local

        value = json.loads(self.manifest.read_text())
        value["deployments"]["cloudflare"]["production"] = {
            key: "https://production.synthetic.invalid" for key in value["deployments"]["cloudflare"]["local"]
        }
        path = self.directory / "production-brand.json"
        path.write_text(json.dumps(value))
        output = self.directory / "production-stage"
        proof = stage(
            path,
            "cloudflare",
            value["identifiers"]["macos_binary_name"],
            output,
            deployment_stage="production",
            distribution="production",
        )
        self.assertEqual(proof["profile"], "cloudflare.production")
        self.assertEqual(proof["bundle_id"], "test.synthetic.native")
        profile = json.loads((output / "ForkDeployment.json").read_text())
        self.assertEqual(profile["auth_base_url"], "https://production.synthetic.invalid")
        self.assertFalse(profile["allows_env_url_override"])
        self.assertFalse(proof["release_ready"])
        with self.assertRaisesRegex(ValueError, "local"):
            package_local(output)
        value["deployments"]["cloudflare"]["production"]["auth_base"] = "http://production.synthetic.invalid"
        path.write_text(json.dumps(value))
        with self.assertRaises(ProfileError):
            stage(
                path,
                "cloudflare",
                value["identifiers"]["macos_binary_name"],
                self.directory / "bad-production",
                deployment_stage="production",
                distribution="production",
            )

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
