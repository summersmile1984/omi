import Foundation

/// Deployment authority for the desktop client.
///
/// `omi_cloud` preserves the shipped client behavior. `self_hosted` is an
/// explicit operator-owned mode: client-direct vendor services and Omi-managed
/// telemetry/update/auth paths are disabled, so a missing or malformed profile
/// cannot silently opt into those paths.
enum DesktopDeploymentProfile: String, Sendable {
  case omiCloud = "omi_cloud"
  case selfHosted = "self_hosted"

  static func resolve(_ rawValue: String?) -> Self {
    guard let rawValue else { return .omiCloud }
    let normalized = rawValue.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    return Self(rawValue: normalized) ?? .selfHosted
  }
}

enum DesktopDeploymentOriginError: Error, Equatable {
  case missing(String)
  case invalid(String)
  case insecure(String)
  case managed(String)
}

enum DesktopIdentityProvider: String, Sendable {
  case firebase
  case betterAuth = "better_auth"
}

/// Shared egress boundary for the desktop surfaces that can otherwise open a
/// provider-owned connection without going through the configured backend.
enum DesktopModelEgressPolicy {
  static func allowsClientDirectVendorEgress(
    deploymentProfile: DesktopDeploymentProfile
  ) -> Bool {
    deploymentProfile == .omiCloud
  }

  static func allowsBYOK(deploymentProfile: DesktopDeploymentProfile) -> Bool {
    deploymentProfile == .omiCloud
  }

  static func allowsOmiManagedServices(deploymentProfile: DesktopDeploymentProfile) -> Bool {
    deploymentProfile == .omiCloud
  }
}

enum DesktopBackendEnvironment {
  static let productionPythonAPIURL = "https://api.omi.me/"
  static let productionRustBackendURL = "https://desktop-backend-hhibjajaja-uc.a.run.app/"
  static let developmentPythonAPIURL = "https://api.omiapi.com/"
  static let developmentRustBackendURL = "https://desktop-backend-dt5lrfkkoa-uc.a.run.app/"
  /// Public web share origin (conversation / chat / task links). Override with ``OMI_SHARE_BASE_URL``.
  static let productionShareBaseURL = "https://h.omi.me"

  static var deploymentProfile: DesktopDeploymentProfile {
    DesktopDeploymentProfile.resolve(currentEnvironmentValue("OMI_DEPLOYMENT_PROFILE"))
  }

  static var identityProvider: DesktopIdentityProvider {
    identityProvider(deploymentProfile: deploymentProfile)
  }

  static func identityProvider(deploymentProfile: DesktopDeploymentProfile) -> DesktopIdentityProvider {
    deploymentProfile == .selfHosted ? .betterAuth : .firebase
  }

  static var shouldConfigureFirebaseSDK: Bool {
    shouldConfigureFirebaseSDK(deploymentProfile: deploymentProfile)
  }

  static func shouldConfigureFirebaseSDK(deploymentProfile: DesktopDeploymentProfile) -> Bool {
    identityProvider(deploymentProfile: deploymentProfile) == .firebase
  }

  static var allowsOmiManagedServices: Bool {
    DesktopModelEgressPolicy.allowsOmiManagedServices(deploymentProfile: deploymentProfile)
  }

  static var shouldUseDevelopmentBackends: Bool {
    shouldUseDevelopmentBackends(
      bundleIdentifier: AppBuild.bundleIdentifier,
      updateChannel: AppBuild.currentUpdateChannel,
      externalPreviewBackend: AppBuild.externalPreviewBackend
    )
  }

  static func shouldUseDevelopmentBackends(
    bundleIdentifier: String,
    updateChannel: String,
    externalPreviewBackend: AppBuild.ExternalPreviewBackend? = nil
  ) -> Bool {
    // External previews opt into their backend through signed bundle metadata. They must
    // never inherit local-development routing or an environment force override. Missing or
    // malformed preview metadata therefore fails closed to the production backend.
    if AppBuild.isExternalPreviewBundleIdentifier(bundleIdentifier) {
      return externalPreviewBackend == .development
    }

    // Beta is the production-account dogfood channel: it uses the development
    // serving plane while retaining production Auth/Firebase/Firestore. This is
    // an identity-bound routing rule, never an update-channel or environment
    // override. Stable remains pinned to the production serving plane.
    if bundleIdentifier == AppBuild.betaProductionBundleIdentifier {
      return true
    }

    // Named/dev bundles route to the dev backend by default. Explicit launch
    // URLs still win below so local harnesses and intentionally-targeted tests
    // remain possible.
    if !AppBuild.productionFamilyBundleIdentifiers.contains(bundleIdentifier) {
      return true
    }

    return false
  }

  static var shouldUseProductionAuth: Bool {
    shouldUseProductionAuth(bundleIdentifier: AppBuild.bundleIdentifier)
  }

  static func shouldUseProductionAuth(bundleIdentifier: String) -> Bool {
    // The shared Firebase project and registered OAuth callback live on the
    // production authority. Beta must never inherit a dev auth override while
    // its data-serving endpoints intentionally target development.
    AppBuild.productionFamilyBundleIdentifiers.contains(bundleIdentifier)
  }

  static var shouldForceDevelopmentServingEndpoints: Bool {
    shouldForceDevelopmentServingEndpoints(bundleIdentifier: AppBuild.bundleIdentifier)
  }

  static func shouldForceDevelopmentServingEndpoints(bundleIdentifier: String) -> Bool {
    bundleIdentifier == AppBuild.betaProductionBundleIdentifier
  }

  static func pythonBaseURL(
    environmentValue: String? = currentEnvironmentValue("OMI_PYTHON_API_URL"),
    deploymentProfile: DesktopDeploymentProfile = DesktopBackendEnvironment.deploymentProfile
  ) -> String {
    pythonBaseURL(
      useDevelopmentBackends: shouldUseDevelopmentBackends,
      environmentValue: environmentValue,
      deploymentProfile: deploymentProfile
    )
  }

  static func pythonBaseURL(
    useDevelopmentBackends: Bool,
    bundleIdentifier: String = AppBuild.bundleIdentifier,
    environmentValue: String?,
    deploymentProfile: DesktopDeploymentProfile = DesktopBackendEnvironment.deploymentProfile
  ) -> String {
    if deploymentProfile == .selfHosted {
      // Self-hosted builds must be explicitly pointed at an operator-owned
      // backend. An absent URL yields an unusable endpoint rather than a
      // silent fallback to api.omi.me.
      return requiredSelfHostedURL(
        environmentValue,
        key: "OMI_PYTHON_API_URL",
        requiresHTTPS: AppBuild.productionFamilyBundleIdentifiers.contains(bundleIdentifier)
      )
    }
    // A production-family app must not allow a launch environment or bundled
    // config to switch its customer data plane. Development identities retain
    // their explicit override seam for local and signed-preview testing.
    if shouldForceDevelopmentServingEndpoints(bundleIdentifier: bundleIdentifier) {
      return developmentPythonAPIURL
    }
    if shouldUseProductionAuth(bundleIdentifier: bundleIdentifier) {
      return productionPythonAPIURL
    }
    if !useDevelopmentBackends {
      return productionPythonAPIURL
    }
    if let url = normalizedURL(environmentValue) {
      return url
    }

    return developmentPythonAPIURL
  }

  static func authBaseURL(
    useDevelopmentBackends: Bool = shouldUseDevelopmentBackends,
    bundleIdentifier: String = AppBuild.bundleIdentifier,
    environmentValue: String? = currentEnvironmentValue("OMI_AUTH_API_URL"),
    deploymentProfile: DesktopDeploymentProfile = DesktopBackendEnvironment.deploymentProfile
  ) -> String {
    if deploymentProfile == .selfHosted {
      let configured =
        normalizedURL(environmentValue)
        ?? normalizedURL(currentEnvironmentValue("OMI_AUTH_SERVER_URL"))
        ?? normalizedURL(currentEnvironmentValue("OMI_AUTH_API_URL"))
      return requiredSelfHostedURL(
        configured,
        key: "OMI_AUTH_SERVER_URL",
        requiresHTTPS: AppBuild.productionFamilyBundleIdentifiers.contains(bundleIdentifier)
      )
    }
    if shouldUseProductionAuth(bundleIdentifier: bundleIdentifier) || !useDevelopmentBackends {
      return productionPythonAPIURL
    }
    if let url = normalizedURL(environmentValue) {
      return url
    }

    // Desktop Apple Sign-In uses the shared Services ID. The registered web
    // callback is on api.omi.me, so beta must not inherit the dev data backend
    // host for OAuth unless a local/dev auth URL is explicitly supplied.
    return productionPythonAPIURL
  }

  static func rustBackendURL(
    environmentValue: String? = currentEnvironmentValue("OMI_DESKTOP_API_URL"),
    launchEnvironmentValue: String? = ProcessInfo.processInfo.environment["OMI_DESKTOP_API_URL"],
    deploymentProfile: DesktopDeploymentProfile = DesktopBackendEnvironment.deploymentProfile
  ) -> String {
    rustBackendURL(
      useDevelopmentBackends: shouldUseDevelopmentBackends,
      environmentValue: environmentValue,
      launchEnvironmentValue: launchEnvironmentValue,
      deploymentProfile: deploymentProfile
    )
  }

  static func rustBackendURL(
    useDevelopmentBackends: Bool,
    bundleIdentifier: String = AppBuild.bundleIdentifier,
    environmentValue: String?,
    launchEnvironmentValue: String?,
    deploymentProfile: DesktopDeploymentProfile = DesktopBackendEnvironment.deploymentProfile
  ) -> String {
    if deploymentProfile == .selfHosted {
      return requiredSelfHostedURL(
        environmentValue ?? launchEnvironmentValue,
        key: "OMI_DESKTOP_API_URL",
        requiresHTTPS: AppBuild.productionFamilyBundleIdentifiers.contains(bundleIdentifier)
      )
    }
    if shouldForceDevelopmentServingEndpoints(bundleIdentifier: bundleIdentifier) {
      return developmentRustBackendURL
    }
    if shouldUseProductionAuth(bundleIdentifier: bundleIdentifier) {
      return productionRustBackendURL
    }
    if !useDevelopmentBackends {
      return productionRustBackendURL
    }
    if let url = normalizedURL(environmentValue) {
      return url
    }

    if let url = normalizedURL(launchEnvironmentValue) {
      return url
    }

    return developmentRustBackendURL
  }

  /// Public share origin used when minting conversation links (#4339).
  /// Matches backend ``OMI_SHARE_BASE_URL`` (default ``https://h.omi.me``).
  static func shareBaseURL(
    environmentValue: String? = currentEnvironmentValue("OMI_SHARE_BASE_URL")
  ) -> String {
    guard var raw = environmentValue?.trimmingCharacters(in: .whitespacesAndNewlines),
      !raw.isEmpty
    else {
      return productionShareBaseURL
    }
    if !raw.contains("://") {
      raw = "https://\(raw)"
    }
    while raw.hasSuffix("/") {
      raw.removeLast()
    }
    guard let url = URL(string: raw),
      let scheme = url.scheme?.lowercased(),
      ["http", "https"].contains(scheme),
      let host = url.host,
      !host.isEmpty
    else {
      return productionShareBaseURL
    }
    return raw
  }

  static func conversationShareURL(
    id: String,
    environmentValue: String? = currentEnvironmentValue("OMI_SHARE_BASE_URL")
  ) -> String {
    "\(shareBaseURL(environmentValue: environmentValue))/conversations/\(id)"
  }

  static func applyReleaseChannelDefaults() {
    guard deploymentProfile != .selfHosted else {
      log("BackendEnvironment: self-hosted profile keeps operator backend URLs unchanged")
      return
    }
    if shouldUseDevelopmentBackends {
      if normalizedURL(currentEnvironmentValue("OMI_PYTHON_API_URL")) == nil {
        setenv("OMI_PYTHON_API_URL", developmentPythonAPIURL, 1)
      }
      if normalizedURL(currentEnvironmentValue("OMI_DESKTOP_API_URL")) == nil {
        setenv("OMI_DESKTOP_API_URL", developmentRustBackendURL, 1)
      }
    }
    log("BackendEnvironment: release-channel defaults applied only for missing backend URLs")
  }

  /// Canonicalize an operator-owned origin before it becomes an API base URL.
  /// Only an origin is accepted: path/query/fragment/credentials are rejected
  /// so callers cannot accidentally join routes across authorities.
  static func canonicalSelfHostedOrigin(
    _ raw: String?,
    key: String,
    requiresHTTPS: Bool
  ) throws -> String {
    guard let raw = nonEmptyValue(raw) else { throw DesktopDeploymentOriginError.missing(key) }
    guard var components = URLComponents(string: raw),
      let rawScheme = components.scheme,
      let rawHost = components.host,
      components.user == nil,
      components.password == nil,
      components.query == nil,
      components.fragment == nil,
      components.path.isEmpty || components.path == "/"
    else {
      throw DesktopDeploymentOriginError.invalid(key)
    }

    let scheme = rawScheme.lowercased()
    guard scheme == "http" || scheme == "https" else {
      throw DesktopDeploymentOriginError.invalid(key)
    }
    if requiresHTTPS, scheme != "https" {
      throw DesktopDeploymentOriginError.insecure(key)
    }

    let host = rawHost.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
    guard !host.isEmpty else { throw DesktopDeploymentOriginError.invalid(key) }
    if host == "desktop-backend-hhibjajaja-uc.a.run.app"
      || host == "desktop-backend-dt5lrfkkoa-uc.a.run.app"
      || host == "omi.me"
      || host.hasSuffix(".omi.me")
      || host == "omiapi.com"
      || host.hasSuffix(".omiapi.com")
    {
      throw DesktopDeploymentOriginError.managed(key)
    }

    components.scheme = scheme
    components.host = host
    components.path = "/"
    if (scheme == "https" && components.port == 443) || (scheme == "http" && components.port == 80) {
      components.port = nil
    }
    guard let url = components.url else { throw DesktopDeploymentOriginError.invalid(key) }
    return url.absoluteString
  }

  private static func requiredSelfHostedURL(_ raw: String?, key: String, requiresHTTPS: Bool) -> String {
    do {
      return try canonicalSelfHostedOrigin(raw, key: key, requiresHTTPS: requiresHTTPS)
    } catch {
      log("BackendEnvironment: self-hosted \(key) rejected (\(String(reflecting: error)))")
      return ""
    }
  }

  private static func nonEmptyValue(_ raw: String?) -> String? {
    guard let value = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else {
      return nil
    }
    return value
  }

  private static func normalizedURL(_ raw: String?) -> String? {
    guard let raw else { return nil }
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else { return nil }
    return trimmed.hasSuffix("/") ? trimmed : trimmed + "/"
  }

  private static func currentEnvironmentValue(_ key: String) -> String? {
    guard let value = getenv(key), let string = String(validatingCString: value) else {
      return nil
    }
    return string
  }
}
