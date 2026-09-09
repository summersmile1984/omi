#!/usr/bin/env python3
"""Stage the existing macOS app with a selected native identity owner.

Deployment stage and application identity are independent: a named verification
app may connect to production. Distribution identities require the matching
stage and the separate Developer ID packaging boundary. Sparkle stays disabled.
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

from swift_overlay import declarations, load_owners, replace_literal, rewrite_functions, rewrite_region, verify_owner

ROOT = Path(__file__).resolve().parents[3]
FORK = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts/profiles"))
from render import resolve  # noqa: E402
from manifest import load_manifest  # noqa: E402

AUTH_REPLACEMENTS = {
    "configure()": """func configure() async {
    guard !isConfigured else { return }
    isConfigured = true
    await forkRestore(attempt: beginSessionAttempt())
  }""",
    "retryRestoredSession()": """func retryRestoredSession() async {
    await forkRestore(attempt: beginSessionAttempt())
  }""",
    "getIdToken(forceRefresh:)": """func getIdToken(forceRefresh: Bool = false) async throws -> String {
    try await forkAccessToken(forceRefresh: forceRefresh)
  }""",
    "performLightSessionInvalidation()": """func performLightSessionInvalidation() async -> Bool {
    await forkInvalidate()
  }""",
    "signInWithApple()": """func signInWithApple() async throws {
    throw NativeAuthFailure.rejected(501)
  }""",
    "signInWithGoogle()": """func signInWithGoogle() async throws {
    throw NativeAuthFailure.rejected(501)
  }""",
    "bootstrapLocalHarnessAuthIfNeeded()": """func bootstrapLocalHarnessAuthIfNeeded() async {
    await configure()
  }""",
    "handleOAuthCallback(url:)": """func handleOAuthCallback(url: URL) {
    error = "OAuth is not configured for this deployment."
  }""",
    "cancelSignIn()": """func cancelSignIn() {
    _ = beginSessionAttempt()
    isLoading = false
  }""",
    "clearTokens()": """func clearTokens() {
    forkNativeState.client.invalidateAccessCache()
    clearUserDefaultsTokens()
  }""",
    "expireStoredTokenForAutomation()": """func expireStoredTokenForAutomation() -> [String: String] {
    forkNativeState.client.invalidateAccessCache()
    return ["provider": "better_auth", "access_cache": "expired"]
  }""",
    "tokenStatusForAutomation()": """func tokenStatusForAutomation() -> [String: String] {
    return ["provider": "better_auth", "profile": ForkDesktopBuild.profile.name,
      "session_stored": ((try? forkNativeState.store.read()) != nil) ? "true" : "false"]
  }""",
}

FIREBASE_START = "    // Initialize Firebase (skipped for local harness"
FIREBASE_END = "    // Initialize analytics (PostHog)"


def stage_memory_batching(source: Path, owners: dict[str, str]) -> None:
    path = source / "Onboarding/OnboardingImportEvidenceService.swift"
    signature = "save(_:logPrefix:authorizationSnapshot:apiClient:sleep:)"
    matches = declarations(path).get(signature, [])
    if len(matches) != 1:
        raise ValueError("Native memory import owner is ambiguous")
    left, right = matches[0]
    method = path.read_bytes()[left:right].decode()
    old = "let chunks = memories.chunked(maxSize: APIClient.memoriesBatchMaxSize)"
    if method.count(old) != 1 or method.count("var failed = 0") != 1:
        raise ValueError("Native memory batch planning owner changed")
    method = method.replace(
        old,
        '''let plan: NativeMemoryBatchPlan<MemoryBatchItem>
    do {
      plan = try NativeMemoryBatching.plan(
        memories, maxCount: APIClient.memoriesBatchMaxSize,
        maxBytes: ForkDesktopBuild.profile.target == "cloudflare" ? NativeMemoryBatching.cloudflareMaxBytes : nil,
        encoder: OmiHTTPTransport.makeEncoder())
    } catch {
      log("Memory import: unable to encode batch requests")
      return (0, memories.count)
    }
    let chunks = plan.batches''',
    ).replace("var failed = 0", "var failed = plan.rejectedCount")
    rewrite_functions(path, {signature: method}, owners)


def application_identity(manifest: dict, app_name: str, deployment_stage: str, distribution: str) -> str:
    identities = manifest["identifiers"]
    if deployment_stage not in ("local", "beta", "production"):
        raise ValueError("Select an explicit deployment stage")
    if distribution == "development":
        if not re.fullmatch(r"omi-[a-z0-9][a-z0-9-]*", app_name):
            raise ValueError("A development artifact requires a named omi-* test bundle")
        bundle_id = identities["macos_named_bundle_prefix"] + app_name
        if bundle_id in (identities["macos_bundle_id"], identities["macos_bundle_id_beta"]):
            raise ValueError("A development artifact cannot claim a distribution identity")
    elif distribution in ("production", "beta"):
        if deployment_stage != distribution:
            raise ValueError("A distribution identity must match its deployment stage")
        expected_name = identities["macos_binary_name"] + (" Beta" if distribution == "beta" else "")
        if app_name != expected_name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]*", app_name):
            raise ValueError("Distribution application name must match the brand binary name")
        bundle_id = identities["macos_bundle_id" if distribution == "production" else "macos_bundle_id_beta"]
    else:
        raise ValueError("Unknown native distribution identity")
    if bundle_id.startswith("com.omi.") and distribution != "development":
        raise ValueError("A fork distribution cannot claim an upstream application identity")
    if bundle_id in {"com.omi.computer-macos", "com.omi.computer-macos.beta", "com.omi.desktop-dev"}:
        raise ValueError("A fork artifact cannot claim an installed upstream identity")
    return bundle_id


def stage(
    manifest_path: Path,
    target: str,
    app_name: str,
    output: Path,
    *,
    deployment_stage: str = "local",
    distribution: str = "development",
) -> dict:
    if target not in ("self_hosted", "cloudflare"):
        raise ValueError("Select a fork target")
    output = output.resolve()
    if output.exists() or output == ROOT or ROOT in output.parents:
        raise ValueError("Output must be a new isolated directory outside the repository")
    manifest_path = manifest_path.resolve(strict=True)
    manifest = load_manifest(None, ROOT, manifest_path)
    bundle_id = application_identity(manifest, app_name, deployment_stage, distribution)
    resolved = resolve(target, manifest_path=manifest_path, stage=deployment_stage)
    profile = resolved["profiles"][f"{target}.{deployment_stage}"]
    identities = manifest["identifiers"]
    owners = load_owners()
    for name in ("omi_app_icon.png", "omi_menu_bar_icon.png", "herologo.png"):
        path = ROOT / "desktop/macos/Desktop/Sources/Resources" / name
        verify_owner("Resources/" + name, path.read_bytes(), owners)
    desktop = output / "Desktop"
    shutil.copytree(
        ROOT / "desktop/macos/Desktop", desktop, ignore=shutil.ignore_patterns(".build", ".swiftpm", ".DS_Store")
    )
    source = desktop / "Sources"
    rewrite_functions(
        source / "Services/APIClient/APIClient+ScreenFrames.swift",
        {"adjudicateScreenFrames(_:)": (FORK / "overlays/screen_frame_adjudication.swift").read_text()},
        owners,
    )
    generated = source / "ForkNative"
    generated.mkdir()
    for path in (FORK / "Sources/NativeIdentity").glob("*.swift"):
        shutil.copy2(path, generated / path.name)
    for path in (FORK / "overlays").glob("ForkNative*.swift"):
        shutil.copy2(path, generated / path.name)
    stage_memory_batching(source, owners)
    for name in ("SignInView.swift", "DesktopBackendEnvironment.swift"):
        verify_owner(name, (source / name).read_bytes(), owners)
        shutil.copy2(FORK / "overlays" / name, source / name)
    asset_input = output / "brand-asset-input.json"
    asset_input.write_text(json.dumps(manifest["assets"]))
    try:
        subprocess.run(
            ["node", str(FORK / "assets.mjs"), str(manifest_path.parent), str(asset_input), str(output)], check=True
        )
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
    auth = source / "AuthService.swift"
    content = auth.read_bytes()
    ranges = declarations(auth)["signOut(acceptedAccountDeletion:)"]
    if len(ranges) != 1:
        raise ValueError("Auth sign-out owner is ambiguous")
    left, right = ranges[0]
    signout = content[left:right].decode()
    marker = "    let sessionAttempt = beginSessionAttempt()"
    if signout.count(marker) != 1:
        raise ValueError("Auth sign-out attempt owner changed")
    signout = signout.replace(marker, marker + "\n    try await forkRevoke(attempt: sessionAttempt)")
    old = '''          if let auth = configuredFirebaseAuth() {
            try auth.signOut()
          } else {
            log("AuthService: Firebase SDK unavailable; signing out the REST-backed session")
          }'''
    if signout.count(old) != 1:
        raise ValueError("Auth sign-out credential transaction changed")
    signout = signout.replace(old, "          try forkNativeState.store.clear()")
    rewrite_functions(auth, {**AUTH_REPLACEMENTS, "signOut(acceptedAccountDeletion:)": signout}, owners)
    replace_literal(
        auth,
        "  static let shared = AuthService()",
        "  static let shared = AuthService()\n  lazy var forkNativeState = ForkNativeAuthState()",
    )
    app = source / "OmiApp.swift"
    rewrite_region(
        app,
        FIREBASE_START,
        FIREBASE_END,
        "    Task { @MainActor in\n      ForkNativeVerification.register()\n      await AuthService.shared.configure()\n    }\n\n",
        "OmiApp.swift:firebase-bootstrap",
        owners,
    )
    replace_literal(
        app, '"https://bbffa02d948c81ea4dccd36246c7bd20@o4511085999816704.ingest.us.sentry.io/4511086024851456"', '""'
    )
    rewrite_functions(
        app,
        {
            "windowTitle(displayName:version:launchMode:isNonProduction:)": '''static func windowTitle(displayName: String, version: String, launchMode: LaunchMode, isNonProduction: Bool) -> String {
    let title = launchMode == .rewind ? "\\(displayName) Rewind" : displayName
    return version.isEmpty ? title : "\\(title) v\\(version)"
  }'''
        },
        owners,
    )
    rewrite_functions(
        source / "BundleEnvironment.swift",
        {"loadIfNeeded()": "static func loadIfNeeded() { ForkDesktopBuild.installEnvironment() }"},
        owners,
    )
    # A fork cannot enroll itself in the upstream PostHog project. Telemetry
    # stays off until a brand-owned provider is implemented and qualified.
    rewrite_functions(
        source / "PostHogManager.swift",
        {"initialize()": "func initialize() { isInitialized = false }"},
        owners,
    )
    app_build = source / "AppBuild.swift"
    storage = source / "OmiSupport/DesktopLocalProfile.swift"
    for path in (app_build, storage):
        verify_owner(path.name, path.read_bytes(), owners)
        replace_literal(path, '"com.omi.computer-macos"', json.dumps(identities["macos_bundle_id"]))
        replace_literal(path, '"com.omi.computer-macos.beta"', json.dumps(identities["macos_bundle_id_beta"]))
    replace_literal(
        app_build,
        'bundleIdentifier.hasPrefix("com.omi.")',
        f'bundleIdentifier.hasPrefix({json.dumps(identities["macos_named_bundle_prefix"])})',
    )
    replace_literal(app_build, '"com.omi.desktop-dev"', json.dumps(identities["macos_bundle_id_dev"]))
    replace_literal(app_build, "!isExternalPreview && !isNamedDevelopmentBundle", "false")
    # Production classification must never grant a fork permission to terminate
    # or remove the user's existing upstream app.
    replace_literal(app_build, "bundleIdentifier == productionBundleIdentifier", "false")
    replace_literal(app_build, 'return "omi"', "return " + json.dumps(manifest["brand"]["display_name"]))
    replace_literal(
        app_build,
        '"https://github.com/BasedHardware/omi/releases"',
        json.dumps("https://github.com/" + manifest["distribution"]["github_releases_repo"] + "/releases"),
    )
    replace_literal(storage, '"com.omi.omi-"', json.dumps(identities["macos_named_bundle_prefix"] + "omi-"))
    replace_literal(storage, "localProfileEnabled || isNamedDevelopmentBundle || isBetaProductionBundle", "true")
    replace_literal(storage, '["Omi Beta"]', json.dumps([manifest["brand"]["id"] + " Beta"]))
    replace_literal(
        storage, 'localProfileStorageName ?? "Omi"', "localProfileStorageName ?? " + json.dumps(manifest["brand"]["id"])
    )
    replace_literal(storage, '["Omi"]', json.dumps([manifest["brand"]["id"]]))
    replace_literal(
        storage,
        '["Omi Dev Bundles", bundleIdentifier]',
        f'[{json.dumps(manifest["brand"]["id"] + " Dev Bundles")}, bundleIdentifier]',
    )
    # Resource access is explicit through the app bundle; SwiftPM's module
    # resource bundle still carries all untouched upstream UI resources.
    (output / "ForkDeployment.json").write_text(json.dumps(profile, indent=2) + "\n")
    swift = f'''import Foundation
enum ForkDesktopBuild {{
  static let productName = {json.dumps(manifest["brand"]["display_name"], ensure_ascii=False)}
  static let tagline = {json.dumps(manifest["brand"].get("tagline", ""), ensure_ascii=False)}
  static let keychainPrefix = {json.dumps(identities["keychain_service_prefix"])}
  static let expectedBundleID = {json.dumps(bundle_id)}
  static let profile: NativeDeploymentProfile = {{
    guard Bundle.main.bundleIdentifier == expectedBundleID,
      let path = Bundle.main.url(forResource: "ForkDeployment", withExtension: "json"),
      let data = try? Data(contentsOf: path),
      let profile = try? NativeDeploymentProfile.decode(data), profile.name == {json.dumps(profile["name"])}
    else {{ fatalError("Invalid deployment bundle") }}
    return profile
  }}()
  static func installEnvironment() {{
    for key in ["OMI_AUTH_API_TOKEN", "OMI_DESKTOP_LOCAL_PROFILE", "FIREBASE_AUTH_EMULATOR_HOST", "FIREBASE_API_KEY", "FIREBASE_PROJECT_ID"] {{ unsetenv(key) }}
    for (key, value) in ["OMI_PYTHON_API_URL": profile.apiBaseURL.absoluteString,
      "OMI_DESKTOP_API_URL": profile.apiBaseURL.absoluteString,
      "OMI_AUTH_API_URL": profile.authBaseURL.absoluteString,
      "OMI_SHARE_BASE_URL": profile.shareBaseURL.absoluteString] {{ setenv(key, value, 1) }}
  }}
}}
'''
    (generated / "ForkDesktopBuild.swift").write_text(swift)
    proof = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "app_name": app_name,
        "brand": manifest["brand"]["id"],
        "product_name": manifest["brand"]["display_name"],
        "tagline": manifest["brand"].get("tagline", ""),
        "distribution": distribution,
        "signing_team": identities["apple_team_id"],
        "assets": json.loads((output / "brand-assets.json").read_text()),
        "profile": profile["name"],
        "release_ready": False,
        "source_owners": owners,
        "output": str(output),
        "staged_sources": {
            str(path.relative_to(desktop)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(source.rglob("*.swift"))
        },
    }
    (output / "stage-manifest.json").write_text(json.dumps(proof, indent=2) + "\n")
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--stage", choices=("local", "beta", "production"), default="local")
    parser.add_argument("--distribution", choices=("development", "beta", "production"), default="development")
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            stage(
                args.manifest,
                args.target,
                args.app_name,
                args.output,
                deployment_stage=args.stage,
                distribution=args.distribution,
            ),
            indent=2,
        )
    )
