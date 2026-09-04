#!/usr/bin/env python3
"""Compile reviewed upstream Dart owners into an isolated local Android artifact.

LIFECYCLE: permanent
The product's HTTP retry, account cutover and provider reset owners are retained.
Opaque native credentials enter at the existing AuthTokenGateway boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
FORK = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts/profiles"))
from render import resolve  # noqa: E402
from manifest import load_manifest  # noqa: E402

AUTH_KEEP = """_instance instance _internal forTesting _maxRefreshAttempts _defaultRefreshAttemptTimeout
_refreshRetryDelays _defaultRefreshDelay _tokenGateway _refreshAttemptTimeout _refreshDelay _recordTelemetry
_telemetryContextProvider _sessionExpiredController _refreshInFlight _expireSessionInFlight _sessionExpired
_sessionGeneration _refreshUserUid sessionExpiredEvents _recordProductionTelemetry _productionTelemetryContext
isSignedIn signOut _invalidateRefreshes handleAuthUserChanged markAuthenticatedUser _clearCachedAuth
_clearCachedIdentityAndAuth getIdToken refreshIdToken _refreshIdTokenWithRetries _refreshIdTokenOnce
_recordRefreshFailure recordAuthenticatedRequest401 expireSession _runSessionExpiration restoreOnboardingState
_restoreOnboardingState updateGivenName getFirebaseUser""".split()

INIT = """Future<void> _init() async {
  await SharedPreferencesUtil.init();
  await NativeIdentity.initialize();
  if (NativeIdentity.owner.currentUser == null) SharedPreferencesUtil().clearUserDisplayCache();
  FlutterForegroundTask.initCommunicationPort();
  await ServiceManager.init();
  LimitlessDeviceConnection.realtimeSuppressionPolicy = () => SharedPreferencesUtil().batchModeEnabled;
  await PlatformManager.initializeServices();
  await NotificationChannelStrings.loadAppLocale();
  await NotificationService.instance.initialize();
  final user = NativeIdentity.owner.currentUser;
  if (user != null) AuthService.instance.markAuthenticatedUser(user.uid);
  final isAuth = await resolveStartupAuth(() => AuthService.instance.getIdToken());
  if (isAuth) {
    if (!SharedPreferencesUtil().onboardingCompleted) await AuthService.instance.restoreOnboardingState();
    await AccountCutoverRuntime.instance.bindAuthenticatedOwner(user?.uid);
  }
  initOpus(await opus_flutter.load());
  BleFlutterApi.setUp(BleBridge.instance);
  BleBridge.instance.stateRestoredCallback = (uuids) => Logger.debug('Restored ${uuids.length} BLE peripherals');
  FlutterError.onError = FlutterError.presentError;
  PlatformDispatcher.instance.onError = (error, stack) {
    Logger.debug('Uncaught platform failure: ${error.runtimeType}');
    return true;
  };
  await ServiceManager.instance().start();
}"""


def once(path: Path, before: str, after: str, count: int = 1) -> None:
    content = path.read_text()
    if content.count(before) != count:
        raise ValueError(f"Reviewed source owner changed: {path.name}")
    path.write_text(content.replace(before, after))


def stage(manifest_path: Path, target: str, output: Path, dart: Path) -> dict:
    output = output.resolve()
    if output.exists() or output == ROOT or ROOT in output.parents:
        raise ValueError("Output must be a fresh directory outside the source repository")
    if target not in {"self_hosted", "cloudflare"}:
        raise ValueError("Select a supported fork target")
    manifest_path = manifest_path.resolve(strict=True)
    manifest = load_manifest(None, ROOT, manifest_path)
    if manifest["brand"]["id"] == "omi-upstream":
        raise ValueError("A synthetic/private brand is required; upstream identity is not a white-label fixture")
    row = resolve(target, manifest_path=manifest_path, stage="local")["profiles"][f"{target}.local"]
    package_id = manifest["identifiers"]["android_application_id_dev"] + ".forktest." + target.replace("_", "")
    if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", package_id):
        raise ValueError("Invalid local package identity")
    owners = json.loads((FORK / "source-owners.json").read_text())["files"]
    for name, digest in owners.items():
        if hashlib.sha256((ROOT / "app" / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Upstream source owner changed; review required: {name}")
    paths = subprocess.check_output(["git", "ls-files", "-z", "app"], cwd=ROOT).decode().split("\0")
    app = output / "app"
    app.mkdir(parents=True)
    for relative in filter(None, paths):
        source = ROOT / relative
        if source.is_symlink():
            raise ValueError("Source symlinks are not admitted in a mobile artifact")
        # Never seed operator credentials or caches into an artifact. Frozen
        # dependency locks and the real native sources are copied unchanged.
        if any(part in {".dart_tool", "build", "Pods", ".gradle", "ephemeral"} for part in source.parts):
            continue
        if source.name in {
            ".env",
            ".dev.env",
            "key.properties",
            "local.properties",
            "google-services.json",
            "GoogleService-Info.plist",
        }:
            continue
        if source.suffix in {".p12", ".jks", ".keystore", ".mobileprovision"}:
            continue
        dest = output / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    shutil.copytree(ROOT / "app/lib/fork", app / "lib/fork", dirs_exist_ok=True)
    shutil.copytree(FORK, app / "fork", dirs_exist_ok=True)
    for source, destination in {
        "auth_provider": "lib/providers/auth_provider.dart",
        "auth": "lib/pages/onboarding/auth.dart",
        "env": "lib/env/env.dart",
        "crashlytics_manager": "lib/utils/debugging/crashlytics_manager.dart",
    }.items():
        shutil.copy2(FORK / f"overlays/{source}.dart.txt", app / destination)
    edits = []

    def member(file, name, source, cls=None):
        edits.append({"file": str(app / file), "member": name, "source": source, **({"class": cls} if cls else {})})

    auth = "lib/services/auth_service.dart"
    edits.append({"file": str(app / auth), "keep": ["AuthService"]})
    edits.append({"file": str(app / auth), "class": "AuthService", "keep": AUTH_KEEP})
    member(
        auth, "isSignedIn", "bool isSignedIn() => !_sessionExpired && _tokenGateway.currentUser != null;", "AuthService"
    )
    member(auth, "getFirebaseUser", "AuthUserSnapshot? get currentUser => _tokenGateway.currentUser;", "AuthService")
    member(
        auth,
        "signOut",
        """Future<void> signOut() async {
      await _tokenGateway.signOut();
      // The synchronous owner notification may have started a newer login.
      // Only clear this owner after its remote revocation and durable removal.
      if (_tokenGateway.currentUser != null) return;
      _invalidateRefreshes(); _clearCachedIdentityAndAuth();
    }""",
        "AuthService",
    )
    member(
        auth,
        "_runSessionExpiration",
        """Future<void> _runSessionExpiration() async {
      try { await _invalidateSession(); }
      catch (error) { Logger.debug('Terminal credential cleanup failed: ${error.runtimeType}'); }
      finally { _expireSessionInFlight = null; }
    }""",
        "AuthService",
    )
    member(
        auth,
        "updateGivenName",
        """Future<void> updateGivenName(String fullName) async {
      await (_tokenGateway as IdentityOwner).updateName(fullName);
    }""",
        "AuthService",
    )
    for name in ("_firebaseOptionsForFlavor", "_ensureFirebaseApp", "_firebaseMessagingBackgroundHandler"):
        member("lib/main.dart", name, "")
    member("lib/main.dart", "_init", INIT)
    member(
        "lib/utils/platform/platform_manager.dart",
        "isFCMSupported",
        "bool get isFCMSupported => false;",
        "PlatformManager",
    )
    member(
        "lib/utils/platform/platform_service.dart",
        "isCrashlyticsSupported",
        "static bool get isCrashlyticsSupported => false;",
        "PlatformService",
    )
    for name in ("onError", "onException"):
        member("lib/utils/logger.dart", name, f"void {name}(dynamic error) {{}}", "CrashlyticsTalkerObserver")
    spec = output / "declaration-edits.json"
    spec.write_text(json.dumps(edits))
    packages = ROOT / "app/.dart_tool/package_config.json"
    subprocess.run([str(dart), f"--packages={packages}", str(FORK / "dart_overlay.dart"), str(spec)], check=True)
    spec.unlink()
    auth_path = app / auth
    text = auth_path.read_text()
    text = re.sub(
        r"import '(?:dart:convert|dart:math|package:(?:app_links|crypto|firebase_auth|google_sign_in|http|sign_in_with_apple|url_launcher)/[^']*|package:flutter/services.dart)'(?: as \w+)?;\n",
        "",
        text,
    )
    text = (
        "import 'package:omi/fork/identity/runtime.dart';\nimport 'package:omi/fork/identity/owner.dart';\nimport 'package:omi/fork/identity/credential.dart';\n"
        + text
    )
    auth_path.write_text(text)
    once(auth_path, "_FirebaseAuthTokenGateway()", "NativeIdentity.owner")
    once(
        auth_path,
        "_tokenGateway = NativeIdentity.owner,",
        "_tokenGateway = NativeIdentity.owner,\n        _invalidateSession = NativeIdentity.owner.invalidate,",
    )
    once(
        auth_path,
        "required AuthTokenGateway tokenGateway,",
        "required AuthTokenGateway tokenGateway,\n    Future<void> Function()? invalidateSession,",
    )
    once(
        auth_path,
        "_tokenGateway = tokenGateway,",
        "_tokenGateway = tokenGateway,\n        _invalidateSession = invalidateSession ?? tokenGateway.signOut,",
    )
    once(
        auth_path,
        "final AuthTokenGateway _tokenGateway;",
        "final AuthTokenGateway _tokenGateway;\n  final Future<void> Function() _invalidateSession;",
    )
    start = text.index("    } on FirebaseAuthException catch (e) {")
    end = text.index("    } catch (e) {", start)
    before = text[start:end]
    once(
        auth_path,
        before,
        """    } on IdentityException catch (e) {
      if (generation != _sessionGeneration) return const AuthTokenMissingUser();
      if (e.failure == IdentityFailure.unauthorized) {
        _clearCachedAuth(); return const AuthTokenTerminalFailure(code: 'session_revoked');
      }
      return AuthTokenTransientFailure(failureClass: e.failure.name);
""",
    )
    main = app / "lib/main.dart"
    content = main.read_text()
    content = re.sub(
        r"import 'package:(?:firebase_[^/]+/[^']+|omi/(?:firebase_options_[^']+|startup_firebase.dart|startup_routing.dart|env/(?:dev_env|prod_env|environment_profile).dart))'(?: as \w+)?;\n",
        "",
        content,
    )
    content = re.sub(
        r"import 'package:(?:awesome_notifications/awesome_notifications.dart|omi/(?:services/notifications/(?:action_item_notification_handler|important_conversation_notification_handler|merge_notification_handler).dart|utils/(?:debugging/crashlytics_manager|environment_detector).dart))';\n",
        "",
        content,
    )
    content = re.sub(
        r"if \(Firebase.apps.isNotEmpty\) \{\s*FirebaseCrashlytics.instance.recordError\(error, stack, fatal: true\);\s*\}",
        "Logger.debug('Unhandled startup failure: ${error.runtimeType}');",
        content,
    )
    content = content.replace("FirebaseAuth.instance.currentUser", "NativeIdentity.owner.currentUser").replace(
        "(resumeUser != null && !resumeUser.isAnonymous)", "(resumeUser != null)"
    )
    main.write_text("import 'package:omi/fork/identity/runtime.dart';\n" + content)
    once(app / "lib/pages/onboarding/wrapper.dart", "import 'package:firebase_auth/firebase_auth.dart';", "")
    once(
        app / "lib/pages/onboarding/wrapper.dart",
        "FirebaseAuth.instance.currentUser!",
        "AuthService.instance.currentUser!",
        3,
    )
    change_name = app / "lib/pages/settings/change_name_widget.dart"
    once(
        change_name,
        "import 'package:firebase_auth/firebase_auth.dart';",
        "import 'package:omi/services/auth/auth_token_result.dart';",
    )
    once(change_name, "User? user", "AuthUserSnapshot? user")
    once(change_name, "AuthService.instance.getFirebaseUser()", "AuthService.instance.currentUser")
    once(change_name, "bool isSaving = false;", "bool isSaving = false;\n  String? saveError;")
    once(
        change_name,
        "const SizedBox(height: 24),",
        "if (saveError != null) Text(saveError!, key: const ValueKey('identity_name_error'), style: const TextStyle(color: Colors.red)),\n            const SizedBox(height: 24),",
    )
    once(change_name, "setState(() => isSaving = true);", "setState(() { isSaving = true; saveError = null; });")
    once(
        change_name,
        "child: TextField(\n",
        "child: TextField(\n                key: const ValueKey('identity_name_edit'),\n",
    )
    once(
        change_name,
        ": () {\n                            if (nameController",
        ": () async {\n                            if (nameController",
    )
    once(
        change_name,
        """                            SharedPreferencesUtil().givenName = nameController.text.trim();
                            AuthService.instance.updateGivenName(nameController.text.trim());
                            AppSnackbar.showSnackbar(context.l10n.nameUpdatedSuccessfully);
                            Navigator.of(context).pop();""",
        """                            try {
                              await AuthService.instance.updateGivenName(nameController.text.trim());
                              if (!mounted) return;
                              AppSnackbar.showSnackbar(context.l10n.nameUpdatedSuccessfully);
                              Navigator.of(context).pop();
                            } catch (_) {
                              if (mounted) setState(() => saveError = context.l10n.connectionError);
                            } finally {
                              if (mounted) setState(() => isSaving = false);
                            }""",
    )
    name_step = app / "lib/pages/onboarding/name/name_widget.dart"
    once(
        name_step,
        "import 'package:flutter/material.dart';",
        "import 'package:flutter/material.dart';\nimport 'package:omi/utils/alerts/app_snackbar.dart';",
    )
    once(name_step, "bool hasPrefilledName = false;", "bool hasPrefilledName = false;\n  bool isSaving = false;")
    once(
        name_step,
        "onPressed: nameController.text.trim().isEmpty",
        "onPressed: isSaving || nameController.text.trim().isEmpty",
    )
    once(
        name_step,
        """                            AuthService.instance.updateGivenName(nameController.text.trim());
                            widget.goNext();""",
        """                            setState(() => isSaving = true);
                            try {
                              await AuthService.instance.updateGivenName(nameController.text.trim());
                              if (mounted) widget.goNext();
                            } catch (_) {
                              if (mounted) AppSnackbar.showSnackbarError(context.l10n.connectionError);
                            } finally {
                              if (mounted) setState(() => isSaving = false);
                            }""",
    )
    settings = app / "lib/pages/settings/settings_drawer.dart"
    content = settings.read_text()
    pattern = r"final rootCtx = globalNavigatorKey.currentContext;\s*if \(rootCtx != null && rootCtx.mounted\) \{\s*clearAllUserState\(rootCtx\);\s*\}\s*await SharedPreferencesUtil\(\).clear\(\);\s*await AuthService.instance.signOut\(\);\s*if \(rootCtx != null && rootCtx.mounted\) \{\s*routeToPage\(rootCtx, const AppShell\(\), replace: true\);\s*\}"
    content, count = re.subn(
        pattern,
        """final rootCtx = globalNavigatorKey.currentContext;
      try {
        final completed = await completeNativeSignOut(
          revoke: AuthService.instance.signOut,
          owner: NativeIdentity.owner,
          clearLocalState: () async {
            if (rootCtx != null && rootCtx.mounted) clearAllUserState(rootCtx);
            await SharedPreferencesUtil().clear();
          });
        if (completed && rootCtx != null && rootCtx.mounted) {
          routeToPage(rootCtx, const AppShell(), replace: true);
        }
      } on IdentityException {
        if (rootCtx != null && rootCtx.mounted) {
          AppSnackbar.showSnackbarError(rootCtx.l10n.connectionError);
        }
      }""",
        content,
    )
    if count != 2:
        raise ValueError("Both settings sign-out transaction owners must be reviewed")
    settings.write_text(
        "import 'package:omi/fork/identity/runtime.dart';\nimport 'package:omi/fork/identity/sign_out.dart';\nimport 'package:omi/fork/identity/credential.dart';\nimport 'package:omi/utils/alerts/app_snackbar.dart';\n"
        + content
    )
    once(
        app / "lib/services/notifications/notification_service.dart",
        "notification_service_fcm.dart",
        "notification_service_basic.dart",
    )
    once(app / "lib/services/notifications/notification_service_basic.dart", "0xFF9D50DD", "0xFFFFFFFF", 2)
    once(app / "lib/utils/logger.dart", "import 'package:firebase_crashlytics/firebase_crashlytics.dart';", "")
    # Retired startup routing has no production caller; don't retain a second
    # profile validator that accepts the official production/mobile-beta plane.
    (app / "lib/startup_routing.dart").unlink()
    native_android(app, package_id, manifest["brand"]["display_name"])
    payload = {
        "profile": row,
        "package_id": package_id,
        "product_name": manifest["brand"]["display_name"],
        "privacy": manifest["domains"]["privacy"],
        "terms": manifest["domains"]["terms"],
    }
    (output / "defines.json").write_text(json.dumps({"OMI_FORK_DEPLOYMENT_JSON": json.dumps(payload)}))
    (app / "lib/flavors.brand.dart").write_text(
        "// generated by app/fork/prepare.py\nconst String kBrandDisplayName = "
        + json.dumps(payload["product_name"])
        + ";\n"
    )
    for env in (".env", ".dev.env"):
        (app / env).write_text("API_BASE_URL=\nUSE_WEB_AUTH=false\nUSE_AUTH_CUSTOM_TOKEN=false\n")
    # The generators may visit unused Firebase declarations. Use the committed
    # emulator declaration for compile-time shape, never runtime configuration.
    for name in ("dev", "prod"):
        shutil.copy2(app / "lib/firebase_options_local.dart", app / f"lib/firebase_options_{name}.dart")
    result = {
        "schema_version": 1,
        "platform": "android",
        "mode": "local_debug_only",
        "package_id": package_id,
        "profile": row,
        "source_owners": owners,
        "artifact": str(app),
        "remote_push": "disabled",
        "oauth": "disabled",
        "release_qualified": False,
    }
    (output / "build-manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def native_android(app: Path, package_id: str, name: str) -> None:
    gradle = app / "android/app/build.gradle"
    content = gradle.read_text()
    content = re.sub(r"^\s*id 'com.google.(?:gms.google-services|firebase.crashlytics)'\n", "\n", content, flags=re.M)
    content = re.sub(r'applicationId "com.friend.ios(?:.dev)?"', 'applicationId ' + json.dumps(package_id), content)
    content = re.sub(
        r'resValue "string", "app_name", "Omi(?: Dev)?"', 'resValue "string", "app_name", ' + json.dumps(name), content
    )
    # This entry cannot build any signed distribution artifact, even when CI
    # happens to have release credentials in its environment.
    content += "\ngradle.taskGraph.whenReady { graph ->\n  if (graph.allTasks.any { it.project.path == ':app' && it.name ==~ /(?i)(assemble|bundle|package|compileFlutterBuild).*(release|profile)/ }) { throw new GradleException('Local fork staging supports debug only') }\n}\n"
    gradle.write_text(content)
    android = "{http://schemas.android.com/apk/res/android}"
    ET.register_namespace("android", "http://schemas.android.com/apk/res/android")
    ET.register_namespace("tools", "http://schemas.android.com/tools")
    manifest = app / "android/app/src/main/AndroidManifest.xml"
    tree = ET.parse(manifest)
    application = tree.getroot().find("application")
    # Native class namespaces are a Pigeon/JNI contract. Application identity
    # changes independently; fully qualify relative class references.
    for element in application.iter():
        value = element.get(android + "name", "")
        if value.startswith("."):
            element.set(android + "name", "com.friend.ios" + value)
    for activity in application.findall("activity"):
        for intent in activity.findall("intent-filter"):
            if intent.find("data") is not None:
                activity.remove(intent)
    ET.SubElement(
        application,
        "meta-data",
        {android + "name": "firebase_data_collection_default_enabled", android + "value": "false"},
    )
    tree.write(manifest, encoding="unicode", xml_declaration=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--target", choices=["self_hosted", "cloudflare"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dart", type=Path, required=True)
    args = parser.parse_args()
    result = stage(args.manifest, args.target, args.output, args.dart)
    print(json.dumps({key: result[key] for key in ("package_id", "platform", "artifact", "mode")}))
