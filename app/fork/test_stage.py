import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fixture import fixture
from prepare import FORK, ROOT, stage


class StageTests(unittest.TestCase):
    def test_target_native_identity_and_real_consumers(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            manifest = directory / "private.json"
            manifest.write_text(json.dumps(fixture(directory)))
            packages = set()
            for target in ["self_hosted", "cloudflare"]:
                output = directory / target
                result = stage(manifest, target, output, Path(os.environ["DART"]))
                self.assertFalse(result["release_qualified"])
                packages.add(result["package_id"])
                defines = json.loads((output / "defines.json").read_text())
                self.assertEqual(set(defines), {"OMI_FORK_DEPLOYMENT_JSON"})
                profile = json.loads(defines["OMI_FORK_DEPLOYMENT_JSON"])["profile"]
                self.assertEqual(profile["name"], target + ".local")
                assets = result["assets"]
                self.assertEqual(set(assets["inputs"]), {"icon_master", "logo_light", "logo_dark", "splash"})
                self.assertEqual(assets["outputs"]["assets/images/herologo.png"]["width"], 256)
                self.assertEqual(
                    assets["outputs"]["android/app/src/main/res/mipmap-xxxhdpi/ic_launcher.png"]["width"], 192
                )
                for path in ("assets/images/herologo.png", "android/app/src/dev/res/mipmap-mdpi/ic_launcher.png"):
                    self.assertNotEqual(assets["outputs"][path]["sha256"], result["source_owners"][path])
                # Static wiring/identity assertions, not behavior-test claims.
                main = (output / "app/lib/main.dart").read_text()
                self.assertIn("NativeIdentity.initialize()", main)
                self.assertNotIn("FirebaseAuth", main)
                self.assertNotIn("FirebaseCrashlytics", main)
                auth = (output / "app/lib/services/auth_service.dart").read_text()
                self.assertIn("_tokenGateway = NativeIdentity.owner", auth)
                self.assertIn("_invalidateSession = NativeIdentity.owner.invalidate", auth)
                self.assertNotIn("FirebaseAuth", auth)
                self.assertNotIn("/auth-issue", (output / "app/lib/providers/auth_provider.dart").read_text())
                notification_service = (output / "app/lib/services/notifications/notification_service.dart").read_text()
                basic_notifications = (
                    output / "app/lib/services/notifications/notification_service_basic.dart"
                ).read_text()
                self.assertIn("notification_service_basic.dart", notification_service)
                self.assertNotIn("notification_service_fcm.dart", notification_service)
                self.assertNotIn("0xFF9D50DD", basic_notifications)
                self.assertIn("0xFFFFFFFF", basic_notifications)
                gradle = (output / "app/android/app/build.gradle").read_text()
                self.assertNotIn('applicationId "com.friend', gradle)
                self.assertIn(result["package_id"], gradle)
                self.assertFalse((output / "app/android/key.properties").exists())
            self.assertEqual(len(packages), 2)

    def test_existing_output_upstream_brand_and_invalid_target_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            manifest = directory / "private.json"
            manifest.write_text(json.dumps(fixture(directory)))
            for target, output, brand in [
                ("self_hosted", directory, manifest),
                ("omi_cloud", directory / "bad", manifest),
                ("self_hosted", directory / "upstream", ROOT / "brand/omi-upstream/manifest.yaml"),
            ]:
                with self.assertRaises(ValueError):
                    stage(brand, target, output, Path(os.environ["DART"]))

    def test_changed_source_owner_is_rejected_before_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            manifest = directory / "private.json"
            manifest.write_text(json.dumps(fixture(directory)))
            actual = Path.read_bytes

            def changed(path):
                data = actual(path)
                return data + b"\n// changed owner\n" if path == ROOT / "app/lib/main.dart" else data

            with patch.object(Path, "read_bytes", changed), self.assertRaisesRegex(ValueError, "source owner changed"):
                stage(manifest, "self_hosted", directory / "stage", Path(os.environ["DART"]))
            self.assertFalse((directory / "stage").exists())

    def test_complete_auth_overlays_do_not_admit_upstream_auth_sources_as_stage_owners(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            manifest = directory / "private.json"
            manifest.write_text(json.dumps(fixture(directory)))
            replaced = {
                ROOT / "app/lib/providers/auth_provider.dart",
                ROOT / "app/lib/pages/onboarding/auth.dart",
            }
            actual = Path.read_bytes

            def changed(path):
                data = actual(path)
                return data + b"\n// unreviewed upstream auth source\n" if path in replaced else data

            with patch.object(Path, "read_bytes", changed):
                result = stage(manifest, "self_hosted", directory / "stage", Path(os.environ["DART"]))

            self.assertNotIn("lib/providers/auth_provider.dart", result["source_owners"])
            self.assertNotIn("lib/pages/onboarding/auth.dart", result["source_owners"])
            self.assertEqual(
                (directory / "stage/app/lib/providers/auth_provider.dart").read_text(),
                (FORK / "overlays/auth_provider.dart.txt").read_text(),
            )
            self.assertEqual(
                (directory / "stage/app/lib/pages/onboarding/auth.dart").read_text(),
                (FORK / "overlays/auth.dart.txt").read_text(),
            )

    def test_invalid_manifest_asset_fails_without_a_partial_mobile_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            value = fixture(directory, "notebook")
            (directory / "assets/notebook-splash.png").write_bytes(b"invalid")
            manifest = directory / "private.json"
            manifest.write_text(json.dumps(value))
            output = directory / "rejected"
            with self.assertRaises(subprocess.CalledProcessError):
                stage(manifest, "cloudflare", output, Path(os.environ["DART"]))
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
