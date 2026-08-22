import Foundation

enum DesktopDeploymentProfile: String, Sendable {
  case omiCloud = "omi_cloud"
  case selfHosted = "self_hosted"

  static func resolve(_ raw: String?) -> DesktopDeploymentProfile {
    guard let raw else { return .omiCloud }
    let normalized = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    // An unknown profile must never inherit the managed-cloud authority. This
    // matters when an operator typo or stale bundle metadata reaches a
    // self-hosted artifact: choosing the restrictive profile keeps every
    // downstream identity/model/connector gate closed until the profile is
    // corrected. Missing metadata remains the legacy cloud default for signed
    // Omi Cloud bundles; packaging gates require an explicit value for
    // self-hosted releases.
    return DesktopDeploymentProfile(rawValue: normalized) ?? .selfHosted
  }
}

enum DesktopIdentityProvider: String, Codable, Sendable {
  case firebase
  case betterAuth = "better_auth"
}

enum DesktopDeploymentOriginError: Error, Equatable {
  case missing(String)
  case invalid(String)
  case insecure(String)
  case managed(String)
}

enum DesktopProactiveModelRoute: Equatable, Sendable {
  case vendorSpecificBackendProxy
  case providerNeutralBackendCapability
}

enum DesktopRealtimeRelaySelection: Equatable, Sendable {
  case cloudPreference
  case backend(provider: String)
  case unavailable
}

/// One deployment-profile boundary for every client-side model credential or
/// vendor socket. The self-hosted artifact delegates provider choice to its
/// configured backend; it never turns a persisted BYOK key, inherited shell
/// variable, or "Auto" preference into permission for client-direct egress.
enum DesktopModelEgressPolicy {
  /// Browser-cookie Google connectors are explicit managed-cloud integrations,
  /// not a deployment-neutral data plane. Self-hosted artifacts expose a
  /// typed unavailable result instead of silently sending cookies to Google.
  static func allowsGoogleBrowserConnectors(
    deploymentProfile: DesktopDeploymentProfile
  ) -> Bool {
    deploymentProfile == .omiCloud
  }

  /// Notion's hosted MCP endpoint is a managed-cloud integration. A
  /// self-hosted artifact must not send exported memories or OAuth material to
  /// it unless an operator-owned connector is added to the signed profile.
  static func allowsHostedNotionConnector(
    deploymentProfile: DesktopDeploymentProfile
  ) -> Bool {
    deploymentProfile == .omiCloud
  }

  static func proactiveRoute(
    deploymentProfile: DesktopDeploymentProfile
  ) -> DesktopProactiveModelRoute {
    deploymentProfile == .selfHosted
      ? .providerNeutralBackendCapability
      : .vendorSpecificBackendProxy
  }

  static func allowsClientDirectVendorEgress(
    deploymentProfile: DesktopDeploymentProfile
  ) -> Bool {
    deploymentProfile == .omiCloud
  }

  /// Onboarding enrichment is an optional public-web capability. The managed
  /// artifact may use its historical DuckDuckGo transport, but a self-hosted
  /// artifact must not silently turn profile-derived queries into direct
  /// third-party traffic. Self-host deployments keep web search behind the
  /// operator backend's explicit SearXNG capability instead.
  static func allowsClientDirectWebSearch(
    deploymentProfile: DesktopDeploymentProfile
  ) -> Bool {
    deploymentProfile == .omiCloud
  }

  static func allowsBYOK(deploymentProfile: DesktopDeploymentProfile) -> Bool {
    deploymentProfile == .omiCloud
  }

  static func allowsAgentAdapter(
    _ adapterID: String,
    deploymentProfile: DesktopDeploymentProfile
  ) -> Bool {
    deploymentProfile == .omiCloud || adapterID == "pi-mono"
  }

  /// Realtime voice remains a backend relay in the self-hosted profile. The
  /// provider must be signed into the artifact; no value means the relay
  /// capability is absent and PTT uses backend-selected pre-recorded STT.
  static func realtimeRelaySelection(
    deploymentProfile: DesktopDeploymentProfile,
    configuredProvider: String?
  ) -> DesktopRealtimeRelaySelection {
    guard deploymentProfile == .selfHosted else { return .cloudPreference }
    guard let raw = configuredProvider?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(),
      !raw.isEmpty
    else {
      return .unavailable
    }
    switch raw {
    case "openai", "gemini":
      return .backend(provider: raw)
    default:
      preconditionFailure("OMI_REALTIME_MODEL_PROVIDER must be openai or gemini")
    }
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
    .resolve(currentEnvironmentValue("OMI_DEPLOYMENT_PROFILE"))
  }

  static var identityProvider: DesktopIdentityProvider {
    identityProvider(
      deploymentProfile: deploymentProfile,
      configuredValue: currentEnvironmentValue("OMI_AUTH_PROVIDER")
    )
  }

  /// A signed deployment profile must never adopt credentials minted by a
  /// different identity system. Missing metadata means a legacy Firebase-only
  /// session and remains compatible with the managed-cloud profile.
  static func acceptsStoredIdentityProvider(
    _ storedProvider: DesktopIdentityProvider?,
    configuredProvider: DesktopIdentityProvider
  ) -> Bool {
    (storedProvider ?? .firebase) == configuredProvider
  }

  /// Omi-operated analytics and update infrastructure is selected only by the
  /// signed cloud profile. Self-hosted artifacts fail closed until their own
  /// provider origins are explicitly added to the deployment contract.
  static var allowsOmiManagedServices: Bool {
    allowsOmiManagedServices(deploymentProfile: deploymentProfile)
  }

  static func allowsOmiManagedServices(deploymentProfile: DesktopDeploymentProfile) -> Bool {
    deploymentProfile == .omiCloud
  }

  /// Managed builds may use FluidAudio's historical first-run model download.
  /// Self-hosted artifacts have no signed model authority in the deployment
  /// profile, so a missing local model must not turn into an implicit
  /// Hugging Face/vendor request. The operator can still provide a future
  /// packaged/local model path once that capability is part of the profile.
  static func allowsImplicitSpeechModelDownload(
    deploymentProfile: DesktopDeploymentProfile
  ) -> Bool {
    deploymentProfile == .omiCloud
  }

  static func shouldConfigureFirebaseSDK(identityProvider: DesktopIdentityProvider) -> Bool {
    identityProvider == .firebase
  }

  static func identityProvider(
    deploymentProfile: DesktopDeploymentProfile,
    configuredValue: String?
  ) -> DesktopIdentityProvider {
    let normalized = configuredValue?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    if let normalized {
      guard let provider = DesktopIdentityProvider(rawValue: normalized) else {
        preconditionFailure("OMI_AUTH_PROVIDER must be firebase or better_auth")
      }
      guard deploymentProfile != .selfHosted || provider == .betterAuth else {
        preconditionFailure("self_hosted requires OMI_AUTH_PROVIDER=better_auth")
      }
      return provider
    }
    return deploymentProfile == .selfHosted ? .betterAuth : .firebase
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
    environmentValue: String? = currentEnvironmentValue("OMI_PYTHON_API_URL")
  ) -> String {
    pythonBaseURL(
      useDevelopmentBackends: shouldUseDevelopmentBackends,
      environmentValue: environmentValue
    )
  }

  static func pythonBaseURL(
    useDevelopmentBackends: Bool,
    bundleIdentifier: String = AppBuild.bundleIdentifier,
    environmentValue: String?,
    deploymentProfile: DesktopDeploymentProfile = deploymentProfile
  ) -> String {
    if deploymentProfile == .selfHosted {
      return requiredSelfHostedURL(
        environmentValue,
        key: "OMI_PYTHON_API_URL",
        requiresHTTPS: AppBuild.productionFamilyBundleIdentifiers.contains(bundleIdentifier))
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
    environmentValue: String? = nil,
    deploymentProfile: DesktopDeploymentProfile = deploymentProfile
  ) -> String {
    if deploymentProfile == .selfHosted {
      let configured = environmentValue ?? currentEnvironmentValue("OMI_AUTH_SERVER_URL")
      return requiredSelfHostedURL(
        configured,
        key: "OMI_AUTH_SERVER_URL",
        requiresHTTPS: AppBuild.productionFamilyBundleIdentifiers.contains(bundleIdentifier))
    }
    let environmentValue = environmentValue ?? currentEnvironmentValue("OMI_AUTH_API_URL")
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

  static func mcpBaseURL(
    useDevelopmentBackends: Bool = shouldUseDevelopmentBackends,
    bundleIdentifier: String = AppBuild.bundleIdentifier,
    environmentValue: String? = currentEnvironmentValue("OMI_MCP_API_URL"),
    deploymentProfile: DesktopDeploymentProfile = deploymentProfile
  ) -> String {
    if deploymentProfile == .selfHosted {
      return requiredSelfHostedURL(
        environmentValue,
        key: "OMI_MCP_API_URL",
        requiresHTTPS: AppBuild.productionFamilyBundleIdentifiers.contains(bundleIdentifier))
    }
    return pythonBaseURL(
      useDevelopmentBackends: useDevelopmentBackends,
      bundleIdentifier: bundleIdentifier,
      environmentValue: environmentValue,
      deploymentProfile: deploymentProfile)
  }

  static func rustBackendURL(
    environmentValue: String? = currentEnvironmentValue("OMI_DESKTOP_API_URL"),
    launchEnvironmentValue: String? = ProcessInfo.processInfo.environment["OMI_DESKTOP_API_URL"]
  ) -> String {
    rustBackendURL(
      useDevelopmentBackends: shouldUseDevelopmentBackends,
      environmentValue: environmentValue,
      launchEnvironmentValue: launchEnvironmentValue
    )
  }

  static func rustBackendURL(
    useDevelopmentBackends: Bool,
    bundleIdentifier: String = AppBuild.bundleIdentifier,
    environmentValue: String?,
    launchEnvironmentValue: String?,
    deploymentProfile: DesktopDeploymentProfile = deploymentProfile
  ) -> String {
    if deploymentProfile == .selfHosted {
      return requiredSelfHostedURL(
        environmentValue ?? launchEnvironmentValue,
        key: "OMI_DESKTOP_API_URL",
        requiresHTTPS: AppBuild.productionFamilyBundleIdentifiers.contains(bundleIdentifier))
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
    environmentValue: String? = currentEnvironmentValue("OMI_SHARE_BASE_URL"),
    deploymentProfile: DesktopDeploymentProfile = deploymentProfile,
    selfHostedBackendURL: String? = nil
  ) -> String {
    let fallback: String
    if deploymentProfile == .selfHosted {
      fallback = stripTrailingSlashes(
        requiredSelfHostedURL(
          selfHostedBackendURL ?? currentEnvironmentValue("OMI_PYTHON_API_URL"),
          key: "OMI_PYTHON_API_URL",
          requiresHTTPS: false))
    } else {
      fallback = productionShareBaseURL
    }
    guard var raw = environmentValue?.trimmingCharacters(in: .whitespacesAndNewlines),
      !raw.isEmpty
    else {
      return fallback
    }
    if deploymentProfile == .selfHosted {
      do {
        return stripTrailingSlashes(
          try canonicalSelfHostedOrigin(raw, key: "OMI_SHARE_BASE_URL", requiresHTTPS: true))
      } catch {
        preconditionFailure("self_hosted deployment requires a canonical OMI_SHARE_BASE_URL: \(error)")
      }
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
      if deploymentProfile == .selfHosted {
        preconditionFailure("self_hosted deployment requires a valid OMI_SHARE_BASE_URL")
      }
      return fallback
    }
    return raw
  }

  static func conversationShareURL(
    id: String,
    environmentValue: String? = currentEnvironmentValue("OMI_SHARE_BASE_URL"),
    deploymentProfile: DesktopDeploymentProfile = deploymentProfile,
    selfHostedBackendURL: String? = nil
  ) -> String {
    let origin = shareBaseURL(
      environmentValue: environmentValue,
      deploymentProfile: deploymentProfile,
      selfHostedBackendURL: selfHostedBackendURL)
    return "\(origin)/conversations/\(id)"
  }

  /// A public MCP OAuth client is optional for self-hosted deployments. Missing
  /// configuration disables that public-OAuth setup path instead of borrowing
  /// an Omi-registered client from the selected backend host.
  static func mcpOAuthClientID(
    environmentValue: String?,
    cloudDefault: String,
    deploymentProfile: DesktopDeploymentProfile = deploymentProfile
  ) -> String? {
    if let configured = nonEmptyValue(environmentValue) { return configured }
    return deploymentProfile == .omiCloud ? cloudDefault : nil
  }

  static func applyReleaseChannelDefaults() {
    if deploymentProfile == .selfHosted {
      let requiresHTTPS = AppBuild.productionFamilyBundleIdentifiers.contains(AppBuild.bundleIdentifier)
      _ = requiredSelfHostedURL(
        currentEnvironmentValue("OMI_PYTHON_API_URL"), key: "OMI_PYTHON_API_URL", requiresHTTPS: requiresHTTPS)
      _ = requiredSelfHostedURL(
        currentEnvironmentValue("OMI_DESKTOP_API_URL"), key: "OMI_DESKTOP_API_URL", requiresHTTPS: requiresHTTPS)
      _ = requiredSelfHostedURL(
        currentEnvironmentValue("OMI_AUTH_SERVER_URL"), key: "OMI_AUTH_SERVER_URL", requiresHTTPS: requiresHTTPS)
      _ = requiredSelfHostedURL(
        currentEnvironmentValue("OMI_MCP_API_URL"), key: "OMI_MCP_API_URL", requiresHTTPS: requiresHTTPS)
      if nonEmptyValue(currentEnvironmentValue("OMI_SHARE_BASE_URL")) != nil {
        _ = requiredSelfHostedURL(
          currentEnvironmentValue("OMI_SHARE_BASE_URL"), key: "OMI_SHARE_BASE_URL", requiresHTTPS: requiresHTTPS)
      }
      guard identityProvider == .betterAuth else {
        preconditionFailure("self_hosted deployment requires OMI_AUTH_PROVIDER=better_auth")
      }
      let realtime = DesktopModelEgressPolicy.realtimeRelaySelection(
        deploymentProfile: .selfHosted,
        configuredProvider: currentEnvironmentValue("OMI_REALTIME_MODEL_PROVIDER"))
      switch realtime {
      case .backend(let provider):
        log("BackendEnvironment: model egress=backend_only realtime_relay=backend provider=\(provider)")
      case .unavailable:
        log("BackendEnvironment: model egress=backend_only realtime_relay=unavailable")
      case .cloudPreference:
        preconditionFailure("self_hosted realtime policy resolved a cloud preference")
      }
      log("BackendEnvironment: validated signed self-hosted deployment profile")
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

  private static func normalizedURL(_ raw: String?) -> String? {
    guard let raw else { return nil }
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else { return nil }
    return trimmed.hasSuffix("/") ? trimmed : trimmed + "/"
  }

  private static func nonEmptyValue(_ raw: String?) -> String? {
    guard let value = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else {
      return nil
    }
    return value
  }

  private static func stripTrailingSlashes(_ raw: String) -> String {
    var value = raw
    while value.hasSuffix("/") { value.removeLast() }
    return value
  }

  static func canonicalSelfHostedOrigin(
    _ raw: String?, key: String, requiresHTTPS: Bool
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
    else { throw DesktopDeploymentOriginError.invalid(key) }

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
      || host == "desktop-backend-dt5lrfkkoa-uc.a.run.app" || host == "omi.me"
      || host.hasSuffix(".omi.me") || host == "omiapi.com" || host.hasSuffix(".omiapi.com")
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
    } catch DesktopDeploymentOriginError.insecure {
      log("BackendEnvironment: self_hosted deployment rejected insecure \(key)")
    } catch {
      log("BackendEnvironment: self_hosted deployment rejected invalid \(key) (reason=\(error))")
    }
    // An invalid operator origin is a typed unavailable route, not permission
    // to recover to an Omi-managed host. Callers treat the empty value as
    // unavailable and stop before constructing a request.
    return ""
  }

  static func currentEnvironmentValue(_ key: String) -> String? {
    guard let value = getenv(key), let string = String(validatingCString: value) else {
      return nil
    }
    return string
  }
}
