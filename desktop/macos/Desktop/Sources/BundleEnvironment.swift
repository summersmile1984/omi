import Foundation

/// Loads bundle `.env` into the process environment before Firebase/auth bootstrap.
enum BundleEnvironment {
  private nonisolated(unsafe) static var didLoad = false
  /// A shipped stable or Beta bundle may take its serving endpoints from the
  /// channel contract, but its Firebase identity is signed into the bundle.
  /// Host .env/launch settings are never an authority for that identity.
  private static let productionFirebaseOverrideKeys: Set<String> = [
    "FIREBASE_API_KEY",
    "FIREBASE_AUTH_EMULATOR_HOST",
    "FIREBASE_PROJECT_ID",
    "OMI_DESKTOP_LOCAL_PROFILE",
  ]
  private static let productionSignedDeploymentKeys: Set<String> = [
    // A production-family bundle's deployment profile is part of its code
    // signature. Host launch variables must not split one release across two
    // identity or data planes; the signed Resources/.env is authoritative.
    "OMI_DEPLOYMENT_PROFILE",
    "OMI_AUTH_PROVIDER",
    "OMI_AUTH_SERVER_URL",
    "OMI_AUTH_API_URL",
    "OMI_PYTHON_API_URL",
    "OMI_DESKTOP_API_URL",
    "OMI_MCP_API_URL",
    "OMI_SHARE_BASE_URL",
    "OMI_REALTIME_MODEL_PROVIDER",
    "OMI_MCP_CHATGPT_OAUTH_CLIENT_ID",
    "OMI_MCP_CLAUDE_OAUTH_CLIENT_ID",
  ]
  /// Capture process-provided values before any bundled environment file is
  /// applied. This makes explicit `open`/launchd overrides authoritative while
  /// retaining the existing merge order between bundled, working-directory,
  /// and user environment files.
  private static let launchEnvironment = ProcessInfo.processInfo.environment

  static func shouldApplyBundledValue(
    for key: String,
    launchEnvironment: [String: String] = BundleEnvironment.launchEnvironment,
    bundleIdentifier: String? = AppBuild.bundleIdentifier
  ) -> Bool {
    if isProductionSignedDeploymentKey(key, bundleIdentifier: bundleIdentifier) { return true }
    guard !isProductionFirebaseOverride(key, bundleIdentifier: bundleIdentifier) else { return false }
    let launchValue = launchEnvironment[key]?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    return launchValue.isEmpty
  }

  static func isProductionFirebaseOverride(_ key: String, bundleIdentifier: String?) -> Bool {
    guard let bundleIdentifier else { return false }
    return AppBuild.productionFamilyBundleIdentifiers.contains(bundleIdentifier)
      && productionFirebaseOverrideKeys.contains(key)
  }

  static func isProductionSignedDeploymentKey(_ key: String, bundleIdentifier: String?) -> Bool {
    guard let bundleIdentifier else { return false }
    return AppBuild.productionFamilyBundleIdentifiers.contains(bundleIdentifier)
      && productionSignedDeploymentKeys.contains(key)
  }

  static func normalizedKey(from assignmentKey: String) -> String? {
    var key = assignmentKey.trimmingCharacters(in: .whitespaces)
    guard key != "export" else { return nil }
    if key.hasPrefix("export ") {
      key = String(key.dropFirst("export ".count)).trimmingCharacters(in: .whitespaces)
    }
    return key.isEmpty ? nil : key
  }

  static func loadIfNeeded() {
    guard !didLoad else { return }
    didLoad = true

    // Clear inherited launchd/shell values before reading any local file. The
    // Beta artifact intentionally uses development serving endpoints, but it
    // shares stable's Firebase Auth and Firestore identity.
    for key in productionFirebaseOverrideKeys.union(productionSignedDeploymentKeys)
    where isProductionFirebaseOverride(key, bundleIdentifier: AppBuild.bundleIdentifier)
      || isProductionSignedDeploymentKey(key, bundleIdentifier: AppBuild.bundleIdentifier)
    {
      unsetenv(key)
    }

    let bundledEnvironmentPath = Bundle.main.path(forResource: ".env", ofType: nil)
    let envPaths = [
      bundledEnvironmentPath,
      FileManager.default.currentDirectoryPath + "/.env",
      NSHomeDirectory() + "/.omi.env",
    ].compactMap { $0 }

    for path in envPaths {
      guard let contents = try? String(contentsOfFile: path, encoding: .utf8) else { continue }
      log("Loading environment from: \(path)")
      for line in contents.components(separatedBy: .newlines) {
        let parts = line.split(separator: "=", maxSplits: 1)
        guard parts.count == 2 else { continue }
        guard let key = normalizedKey(from: String(parts[0])) else { continue }
        guard !key.hasPrefix("#") else { continue }
        if isProductionSignedDeploymentKey(key, bundleIdentifier: AppBuild.bundleIdentifier),
          path != bundledEnvironmentPath
        {
          log("  Skipped \(key) (production deployment values come only from the signed bundle)")
          continue
        }
        let backendServedKeys = ["GEMINI_API_KEY", "GOOGLE_CALENDAR_API_KEY"]
        if backendServedKeys.contains(key) {
          log("  Skipped \(key) (fetched from backend via APIKeyService)")
          continue
        }
        guard shouldApplyBundledValue(for: key) else {
          log("  Skipped \(key) (explicit launch environment override)")
          continue
        }
        let value = String(parts[1]).trimmingCharacters(in: .whitespaces)
          .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
        setenv(key, value, 1)
        if key.contains("API_KEY") || key.contains("KEY") {
          log("  Set \(key)=***")
        }
      }
    }

    DesktopBackendEnvironment.applyReleaseChannelDefaults()
    log("Environment loaded (API keys will be fetched from backend after auth)")
  }
}
