import Foundation

public enum ContextDeploymentMode: String, Codable, Sendable {
  case omiCloud = "omi_cloud"
  case selfHosted = "self_hosted"
}

public enum ContextIdentityProvider: String, Codable, Sendable {
  case firebase
  case betterAuth = "better_auth"
}

public enum ContextSpeechModelAuthority: Equatable, Sendable {
  /// Managed cloud retains FluidAudio's historical HuggingFace download path.
  case managedDownload
  /// Self-hosted models are provisioned by the operator and loaded with
  /// FluidAudio offline mode enabled. The path is bundle-relative in releases.
  case local(path: String)
  /// Transcription is intentionally absent from this artifact.
  case disabled
}

public enum ContextDeploymentProfileError: LocalizedError, Equatable {
  case missing(String)
  case invalidURL(String)
  case insecureReleaseURL(String)
  case managedOrigin(String)
  case invalidMode
  case invalidIdentityProvider
  case invalidSpeechModelAuthority

  public var errorDescription: String? {
    switch self {
    case .missing(let key): return "The signed deployment profile is missing \(key)."
    case .invalidURL(let key): return "The signed deployment profile has an invalid \(key)."
    case .insecureReleaseURL(let key):
      return "The signed release profile requires HTTPS for \(key)."
    case .managedOrigin(let key):
      return "A self-hosted profile cannot use an Omi-operated origin for \(key)."
    case .invalidMode: return "The signed deployment profile must be omi_cloud or self_hosted."
    case .invalidIdentityProvider: return "A self-hosted profile requires Better Auth."
    case .invalidSpeechModelAuthority:
      return
        "A self-hosted profile requires a local speech model or an explicitly disabled capability."
    }
  }
}

/// One signed origin/identity contract shared by the Context app and its MCP
/// helper. Release builds read only Info.plist; DEBUG builds may override with
/// the matching environment variables for local exercise.
public struct ContextDeploymentProfile: Equatable, Sendable {
  public let mode: ContextDeploymentMode
  public let identityProvider: ContextIdentityProvider
  public let backendBaseURL: URL
  public let desktopBaseURL: URL
  public let authBaseURL: URL
  public let mcpBaseURL: URL
  public let speechModelAuthority: ContextSpeechModelAuthority
  /// Optional operator-owned Sparkle appcast. A missing value is a typed
  /// unavailable update capability, not a license to use the managed feed.
  public let updateFeedURL: URL?
  /// Optional base64 Ed25519 public key paired with ``updateFeedURL``.
  public let updatePublicKey: String?

  /// Sessions written before provider metadata existed are Firebase sessions.
  /// A self-hosted Better Auth build must make the user sign in to that
  /// deployment instead of replaying a managed-cloud credential.
  public func acceptsStoredIdentityProvider(_ storedProvider: ContextIdentityProvider?) -> Bool {
    (storedProvider ?? .firebase) == identityProvider
  }

  public static let omiCloud = ContextDeploymentProfile(
    mode: .omiCloud,
    identityProvider: .firebase,
    backendBaseURL: URL(string: "https://api.omi.me/")!,
    desktopBaseURL: URL(string: "https://desktop-backend-hhibjajaja-uc.a.run.app/")!,
    authBaseURL: URL(string: "https://api.omi.me/")!,
    mcpBaseURL: URL(string: "https://api.omi.me/")!,
    speechModelAuthority: .managedDownload,
    updateFeedURL: nil,
    updatePublicKey: nil
  )

  public static let current: ContextDeploymentProfile = {
    do {
      let values = bundleValues()
      let allowsInsecureLocalEndpoints = values["OmiAllowsInsecureLocalEndpoints"] == "true"
      return try resolve(
        bundleValues: values,
        environment: ProcessInfo.processInfo.environment,
        allowsEnvironmentOverrides: allowsEnvironmentOverrides,
        requiresHTTPS: !allowsEnvironmentOverrides && !allowsInsecureLocalEndpoints
      )
    } catch {
      fatalError("Invalid Context for Claude deployment profile: \(error.localizedDescription)")
    }
  }()

  public static func resolve(
    bundleValues: [String: String],
    environment: [String: String] = [:],
    allowsEnvironmentOverrides: Bool,
    requiresHTTPS: Bool? = nil
  ) throws -> ContextDeploymentProfile {
    func value(bundleKey: String, environmentKey: String) -> String? {
      if allowsEnvironmentOverrides,
        let override = environment[environmentKey]?.trimmingCharacters(in: .whitespacesAndNewlines),
        !override.isEmpty
      {
        return override
      }
      let bundled = bundleValues[bundleKey]?.trimmingCharacters(in: .whitespacesAndNewlines)
      return bundled?.isEmpty == false ? bundled : nil
    }

    let rawMode =
      value(bundleKey: "OmiDeploymentProfile", environmentKey: "OMI_DEPLOYMENT_PROFILE")
      ?? ContextDeploymentMode.omiCloud.rawValue
    guard let mode = ContextDeploymentMode(rawValue: rawMode.lowercased()) else {
      throw ContextDeploymentProfileError.invalidMode
    }
    if mode == .omiCloud { return .omiCloud }

    let providerRaw =
      value(bundleKey: "OmiAuthProvider", environmentKey: "OMI_AUTH_PROVIDER")
      ?? ContextIdentityProvider.betterAuth.rawValue
    guard ContextIdentityProvider(rawValue: providerRaw.lowercased()) == .betterAuth else {
      throw ContextDeploymentProfileError.invalidIdentityProvider
    }

    let enforceHTTPS = requiresHTTPS ?? !allowsEnvironmentOverrides
    let backend = try endpoint(
      value(bundleKey: "OmiBackendBaseURL", environmentKey: "OMI_PYTHON_API_URL"),
      key: "OMI_PYTHON_API_URL",
      requiresHTTPS: enforceHTTPS,
      rejectsManagedOrigin: true
    )
    let desktop = try endpoint(
      value(bundleKey: "OmiDesktopBaseURL", environmentKey: "OMI_DESKTOP_API_URL"),
      key: "OMI_DESKTOP_API_URL",
      requiresHTTPS: enforceHTTPS,
      rejectsManagedOrigin: true
    )
    let auth = try endpoint(
      value(bundleKey: "OmiAuthBaseURL", environmentKey: "OMI_AUTH_SERVER_URL"),
      key: "OMI_AUTH_SERVER_URL",
      requiresHTTPS: enforceHTTPS,
      rejectsManagedOrigin: true
    )
    let mcp = try endpoint(
      value(bundleKey: "OmiMCPBaseURL", environmentKey: "OMI_MCP_API_URL")
        ?? backend.absoluteString,
      key: "OMI_MCP_API_URL",
      requiresHTTPS: enforceHTTPS,
      rejectsManagedOrigin: true
    )
    let updateFeedURL = try optionalOperatorUpdateFeedURL(
      value(bundleKey: "OmiUpdateFeedURL", environmentKey: "OMI_UPDATE_FEED_URL"),
      key: "OMI_UPDATE_FEED_URL",
      requiresHTTPS: enforceHTTPS)
    let updatePublicKey = value(
      bundleKey: "OmiUpdatePublicKey", environmentKey: "OMI_UPDATE_PUBLIC_KEY")
    let rawSpeechMode = value(
      bundleKey: "OmiSpeechModelMode", environmentKey: "OMI_SPEECH_MODEL_MODE")?.lowercased()
    let speechModelAuthority: ContextSpeechModelAuthority
    switch rawSpeechMode {
    case "disabled":
      speechModelAuthority = .disabled
    case "local", "packaged":
      guard
        let path = value(bundleKey: "OmiSpeechModelPath", environmentKey: "OMI_SPEECH_MODEL_PATH")
      else { throw ContextDeploymentProfileError.invalidSpeechModelAuthority }
      if !allowsEnvironmentOverrides,
        path.hasPrefix("/") || path.split(separator: "/").contains("..")
      {
        throw ContextDeploymentProfileError.invalidSpeechModelAuthority
      }
      speechModelAuthority = .local(path: path)
    default:
      throw ContextDeploymentProfileError.invalidSpeechModelAuthority
    }
    return ContextDeploymentProfile(
      mode: .selfHosted,
      identityProvider: .betterAuth,
      backendBaseURL: backend,
      desktopBaseURL: desktop,
      authBaseURL: auth,
      mcpBaseURL: mcp,
      speechModelAuthority: speechModelAuthority,
      updateFeedURL: updateFeedURL,
      updatePublicKey: updatePublicKey
    )
  }

  private static var allowsEnvironmentOverrides: Bool {
    #if DEBUG
      true
    #else
      false
    #endif
  }

  private static func bundleValues(bundle: Bundle = .main) -> [String: String] {
    let keys = [
      "OmiDeploymentProfile",
      "OmiAuthProvider",
      "OmiBackendBaseURL",
      "OmiDesktopBaseURL",
      "OmiAuthBaseURL",
      "OmiMCPBaseURL",
      "OmiAllowsInsecureLocalEndpoints",
      "OmiSpeechModelMode",
      "OmiSpeechModelPath",
      "OmiUpdateFeedURL",
      "OmiUpdatePublicKey",
    ]
    var values: [String: String] = [:]
    for key in keys {
      if let value = bundle.object(forInfoDictionaryKey: key) as? String {
        values[key] = value
      } else if let value = bundle.object(forInfoDictionaryKey: key) as? NSNumber {
        values[key] = value.boolValue ? "true" : "false"
      }
    }
    return values
  }

  private static func endpoint(
    _ raw: String?, key: String, requiresHTTPS: Bool, rejectsManagedOrigin: Bool = false
  ) throws -> URL {
    guard let raw else { throw ContextDeploymentProfileError.missing(key) }
    guard var components = URLComponents(string: raw),
      let scheme = components.scheme?.lowercased(), components.host != nil,
      scheme == "http" || scheme == "https", components.user == nil, components.password == nil,
      components.query == nil, components.fragment == nil,
      components.path.isEmpty || components.path == "/"
    else { throw ContextDeploymentProfileError.invalidURL(key) }
    if requiresHTTPS, scheme != "https" {
      throw ContextDeploymentProfileError.insecureReleaseURL(key)
    }
    guard let rawHost = components.host else {
      throw ContextDeploymentProfileError.invalidURL(key)
    }
    let host = rawHost.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
    guard !host.isEmpty else { throw ContextDeploymentProfileError.invalidURL(key) }
    if rejectsManagedOrigin, isOmiOperatedHost(host) {
      throw ContextDeploymentProfileError.managedOrigin(key)
    }
    components.scheme = scheme
    components.host = host
    if (scheme == "https" && components.port == 443) || (scheme == "http" && components.port == 80) {
      components.port = nil
    }
    components.path = "/"
    guard let url = components.url else { throw ContextDeploymentProfileError.invalidURL(key) }
    return url
  }

  /// Canonicalize an operator-owned appcast URL without allowing a pathless
  /// origin, credentials, query/fragment routing, or an Omi-managed host.
  public static func canonicalOperatorUpdateFeedURL(
    _ raw: String,
    requiresHTTPS: Bool = true
  ) -> URL? {
    try? operatorUpdateFeedURL(raw, requiresHTTPS: requiresHTTPS)
  }

  private static func optionalOperatorUpdateFeedURL(
    _ raw: String?, key: String, requiresHTTPS: Bool
  ) throws -> URL? {
    guard let raw else { return nil }
    return try operatorUpdateFeedURL(raw, key: key, requiresHTTPS: requiresHTTPS)
  }

  private static func operatorUpdateFeedURL(
    _ raw: String,
    key: String = "OMI_UPDATE_FEED_URL",
    requiresHTTPS: Bool
  ) throws -> URL {
    guard var components = URLComponents(string: raw),
      let scheme = components.scheme?.lowercased(),
      let rawHost = components.host,
      !rawHost.isEmpty,
      scheme == "http" || scheme == "https",
      components.user == nil,
      components.password == nil,
      components.query == nil,
      components.fragment == nil,
      !components.path.isEmpty,
      components.path != "/"
    else { throw ContextDeploymentProfileError.invalidURL(key) }
    if requiresHTTPS, scheme != "https" {
      throw ContextDeploymentProfileError.insecureReleaseURL(key)
    }
    let host = rawHost.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
    guard !host.isEmpty else { throw ContextDeploymentProfileError.invalidURL(key) }
    if isOmiOperatedHost(host) {
      throw ContextDeploymentProfileError.managedOrigin(key)
    }
    components.scheme = scheme
    components.host = host
    if (scheme == "https" && components.port == 443) || (scheme == "http" && components.port == 80) {
      components.port = nil
    }
    guard let url = components.url else { throw ContextDeploymentProfileError.invalidURL(key) }
    return url
  }

  private static func isOmiOperatedHost(_ host: String) -> Bool {
    host == "desktop-backend-hhibjajaja-uc.a.run.app"
      || host == "desktop-backend-dt5lrfkkoa-uc.a.run.app" || host == "omi.me"
      || host.hasSuffix(".omi.me") || host == "omiapi.com" || host.hasSuffix(".omiapi.com")
  }
}
