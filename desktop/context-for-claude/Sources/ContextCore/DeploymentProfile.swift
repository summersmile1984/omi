import Foundation

public enum ContextDeploymentMode: String, Codable, Sendable {
  case omiCloud = "omi_cloud"
  case selfHosted = "self_hosted"
}

public enum ContextIdentityProvider: String, Codable, Sendable {
  case firebase
  case betterAuth = "better_auth"
}

public enum ContextDeploymentProfileError: LocalizedError, Equatable {
  case missing(String)
  case invalidURL(String)
  case insecureReleaseURL(String)
  case invalidMode
  case invalidIdentityProvider

  public var errorDescription: String? {
    switch self {
    case .missing(let key): return "The signed deployment profile is missing \(key)."
    case .invalidURL(let key): return "The signed deployment profile has an invalid \(key)."
    case .insecureReleaseURL(let key): return "The signed release profile requires HTTPS for \(key)."
    case .invalidMode: return "The signed deployment profile must be omi_cloud or self_hosted."
    case .invalidIdentityProvider: return "A self-hosted profile requires Better Auth."
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
  public let authBaseURL: URL
  public let mcpBaseURL: URL

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
    authBaseURL: URL(string: "https://api.omi.me/")!,
    mcpBaseURL: URL(string: "https://api.omi.me/")!
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
      requiresHTTPS: enforceHTTPS
    )
    let auth = try endpoint(
      value(bundleKey: "OmiAuthBaseURL", environmentKey: "OMI_AUTH_SERVER_URL"),
      key: "OMI_AUTH_SERVER_URL",
      requiresHTTPS: enforceHTTPS
    )
    let mcp = try endpoint(
      value(bundleKey: "OmiMCPBaseURL", environmentKey: "OMI_MCP_API_URL")
        ?? backend.absoluteString,
      key: "OMI_MCP_API_URL",
      requiresHTTPS: enforceHTTPS
    )
    return ContextDeploymentProfile(
      mode: .selfHosted,
      identityProvider: .betterAuth,
      backendBaseURL: backend,
      authBaseURL: auth,
      mcpBaseURL: mcp
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
      "OmiAuthBaseURL",
      "OmiMCPBaseURL",
      "OmiAllowsInsecureLocalEndpoints",
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

  private static func endpoint(_ raw: String?, key: String, requiresHTTPS: Bool) throws -> URL {
    guard let raw else { throw ContextDeploymentProfileError.missing(key) }
    let terminated = raw.hasSuffix("/") ? raw : raw + "/"
    guard let url = URL(string: terminated), let scheme = url.scheme?.lowercased(), url.host != nil,
      scheme == "http" || scheme == "https"
    else { throw ContextDeploymentProfileError.invalidURL(key) }
    if requiresHTTPS, scheme != "https" {
      throw ContextDeploymentProfileError.insecureReleaseURL(key)
    }
    return url
  }
}
