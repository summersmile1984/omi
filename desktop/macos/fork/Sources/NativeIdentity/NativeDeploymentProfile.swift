import Foundation

public struct NativeDeploymentProfile: Codable, Equatable, Sendable {
  public let name: String
  public let target: String
  public let stage: String
  public let identityProvider: String
  public let apiBaseURL: URL
  public let authBaseURL: URL
  public let webBaseURL: URL
  public let mcpBaseURL: URL
  public let shareBaseURL: URL
  public let objectsBaseURL: URL

  enum CodingKeys: String, CodingKey {
    case name, target, stage
    case identityProvider = "identity_provider"
    case apiBaseURL = "api_base_url"
    case authBaseURL = "auth_base_url"
    case webBaseURL = "web_base_url"
    case mcpBaseURL = "mcp_base_url"
    case shareBaseURL = "share_base_url"
    case objectsBaseURL = "objects_base_url"
  }

  public static func decode(_ data: Data) throws -> Self {
    let profile = try JSONDecoder().decode(Self.self, from: data)
    guard ["self_hosted", "cloudflare"].contains(profile.target),
      ["local", "beta", "production"].contains(profile.stage),
      profile.name == "\(profile.target).\(profile.stage)",
      profile.identityProvider == "better_auth"
    else { throw NativeAuthFailure.invalidConfiguration }
    for url in [
      profile.apiBaseURL, profile.authBaseURL, profile.webBaseURL,
      profile.mcpBaseURL, profile.shareBaseURL, profile.objectsBaseURL,
    ] {
      guard let host = url.host, !host.isEmpty,
        url.scheme == "https" || (profile.stage == "local" && url.scheme == "http"),
        url.user == nil, url.password == nil, url.query == nil, url.fragment == nil
      else { throw NativeAuthFailure.invalidConfiguration }
    }
    guard ["", "/"].contains(profile.authBaseURL.path) else {
      throw NativeAuthFailure.invalidConfiguration
    }
    return profile
  }

  public func authURL(_ path: String) -> URL {
    authBaseURL.appendingPathComponent("api/auth").appendingPathComponent(path)
  }

  /// The caller supplies a validated endpoint path; API prefixes are preserved.
  public func realtimeURL(path: String) throws -> URL {
    guard path.hasPrefix("/"), !path.contains("?"), !path.contains("#"),
      var parts = URLComponents(url: apiBaseURL, resolvingAgainstBaseURL: false)
    else { throw NativeAuthFailure.invalidConfiguration }
    parts.scheme = apiBaseURL.scheme == "https" ? "wss" : "ws"
    parts.path = parts.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    parts.path = (parts.path.isEmpty ? "" : "/" + parts.path) + path
    guard let url = parts.url else { throw NativeAuthFailure.invalidConfiguration }
    return url
  }
}

public enum NativeAuthFailure: Error, LocalizedError, Equatable, Sendable {
  case invalidConfiguration
  case unauthorized
  case unavailable
  case invalidResponse
  case rejected(Int)
  case storageUnavailable
  case superseded

  public var errorDescription: String? {
    switch self {
    case .invalidConfiguration: "This application has an invalid deployment configuration."
    case .unauthorized: "Your session has expired. Please sign in again."
    case .unavailable: "Sign-in is temporarily unavailable. Please try again."
    case .invalidResponse: "The identity service returned an invalid response."
    case .rejected: "Unable to sign in. Check your details and try again."
    case .storageUnavailable: "Your session could not be stored securely. Please try again."
    case .superseded: "A newer sign-in or sign-out replaced this request."
    }
  }
}
