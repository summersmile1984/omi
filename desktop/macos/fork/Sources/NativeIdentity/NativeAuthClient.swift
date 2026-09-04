import Foundation

public struct NativeAuthUser: Codable, Equatable, Sendable {
  public let id: String
  public let email: String?
  public let name: String?
  public let image: String?

  public init(id: String, email: String? = nil, name: String? = nil, image: String? = nil) {
    self.id = id
    self.email = email
    self.name = name
    self.image = image
  }
}

/// Persist only this opaque credential, in the selected identity's Keychain item.
/// Access JWTs are deliberately absent from the serialized storage contract.
public struct NativeAuthSession: Codable, Equatable, Sendable {
  public let token: String
  public let user: NativeAuthUser

  public init(token: String, user: NativeAuthUser) {
    self.token = token
    self.user = user
  }
}

private final class NativeAuthRedirectPolicy: NSObject, URLSessionTaskDelegate {
  func urlSession(
    _ session: URLSession, task: URLSessionTask,
    willPerformHTTPRedirection response: HTTPURLResponse,
    newRequest request: URLRequest,
    completionHandler: @escaping (URLRequest?) -> Void
  ) {
    completionHandler(nil)
  }
}

/// Transport/cache only. AuthService's existing attempt and runtime owner fences
/// remain authoritative for publishing credentials, defaults, and signed-in UI.
@MainActor
public final class NativeAuthClient {
  public typealias Transport = @Sendable (URLRequest) async throws -> (Data, HTTPURLResponse)
  private let profile: NativeDeploymentProfile
  private let transport: Transport
  private let now: @Sendable () -> Date
  private var cacheGeneration: UInt64 = 0
  private var access: (session: String, token: String, expires: Date)?
  private var refreshing: (id: UUID, session: String, task: Task<String, Error>)?

  public init(
    profile: NativeDeploymentProfile,
    transport: @escaping Transport = NativeAuthClient.liveTransport(),
    now: @escaping @Sendable () -> Date = Date.init
  ) {
    self.profile = profile
    self.transport = transport
    self.now = now
  }

  nonisolated public static func liveTransport() -> Transport {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.httpCookieStorage = nil
    configuration.httpShouldSetCookies = false
    configuration.urlCache = nil
    configuration.timeoutIntervalForRequest = 20
    let session = URLSession(configuration: configuration, delegate: NativeAuthRedirectPolicy(), delegateQueue: nil)
    return { request in
      let (data, response) = try await session.data(for: request)
      guard let response = response as? HTTPURLResponse else { throw NativeAuthFailure.invalidResponse }
      return (data, response)
    }
  }

  private func request(
    _ path: String, body: [String: String]? = nil, session: String? = nil
  ) async throws -> (Data, HTTPURLResponse) {
    var request = URLRequest(url: profile.authURL(path))
    request.httpMethod = body == nil ? "GET" : "POST"
    request.cachePolicy = .reloadIgnoringLocalCacheData
    request.httpShouldHandleCookies = false
    if let body {
      request.httpBody = try JSONEncoder().encode(body)
      request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    }
    if let session { request.setValue("Bearer \(session)", forHTTPHeaderField: "Authorization") }
    let result: (Data, HTTPURLResponse)
    do {
      result = try await transport(request)
    } catch {
      throw NativeAuthFailure.unavailable
    }
    switch result.1.statusCode {
    case 200..<300: return result
    case 401: throw NativeAuthFailure.unauthorized
    case 500...599: throw NativeAuthFailure.unavailable
    default: throw NativeAuthFailure.rejected(result.1.statusCode)
    }
  }

  public func signIn(email: String, password: String) async throws -> NativeAuthSession {
    try await authenticate("sign-in/email", body: ["email": email, "password": password])
  }

  public func signUp(name: String, email: String, password: String) async throws -> NativeAuthSession {
    try await authenticate("sign-up/email", body: ["name": name, "email": email, "password": password])
  }

  private struct UserResponse: Decodable { let user: NativeAuthUser? }

  private func authenticate(_ path: String, body: [String: String]) async throws -> NativeAuthSession {
    let data: Data
    let response: HTTPURLResponse
    do { (data, response) = try await request(path, body: body) } catch NativeAuthFailure.unauthorized {
      throw NativeAuthFailure.rejected(401)
    }
    guard let user = try JSONDecoder().decode(UserResponse.self, from: data).user, !user.id.isEmpty,
      let token = response.value(forHTTPHeaderField: "set-auth-token"), !token.isEmpty
    else { throw NativeAuthFailure.invalidResponse }
    return NativeAuthSession(token: token, user: user)
  }

  public func restore(_ session: NativeAuthSession) async throws -> NativeAuthSession {
    let (data, _) = try await request("get-session", session: session.token)
    let response: UserResponse?
    do { response = try JSONDecoder().decode(UserResponse?.self, from: data) } catch {
      throw NativeAuthFailure.invalidResponse
    }
    guard let user = response?.user, !user.id.isEmpty, user.id == session.user.id
    else { throw NativeAuthFailure.unauthorized }
    return NativeAuthSession(token: session.token, user: user)
  }

  public func revoke(_ session: NativeAuthSession) async throws {
    do {
      _ = try await request("sign-out", body: [:], session: session.token)
    } catch NativeAuthFailure.unauthorized {
      // Already revoked is a successful logout; outages preserve the credential.
    }
    invalidateAccessCache()
  }

  public func invalidateAccessCache() {
    cacheGeneration &+= 1
    access = nil
    refreshing = nil
  }

  public func accessToken(for session: NativeAuthSession, forceRefresh: Bool = false) async throws -> String {
    if !forceRefresh, let access, access.session == session.token, access.expires.timeIntervalSince(now()) > 60 {
      return access.token
    }
    if let refreshing, refreshing.session == session.token {
      return try await refreshing.task.value
    }
    let id = UUID()
    let generation = cacheGeneration
    let task = Task { try await self.exchange(session, generation: generation) }
    refreshing = (id, session.token, task)
    defer { if refreshing?.id == id { refreshing = nil } }
    return try await task.value
  }

  private struct TokenResponse: Decodable { let token: String }
  private struct Claims: Decodable {
    let sub: String
    let uid: String
    let sid: String
    let iat: Int
    let exp: Int
  }

  private func exchange(_ session: NativeAuthSession, generation: UInt64) async throws -> String {
    let (data, _) = try await request("token", session: session.token)
    let token = try JSONDecoder().decode(TokenResponse.self, from: data).token
    let parts = token.split(separator: ".", omittingEmptySubsequences: false)
    guard parts.count == 3 else { throw NativeAuthFailure.invalidResponse }
    var encoded = String(parts[1]).replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
    encoded += String(repeating: "=", count: (4 - encoded.count % 4) % 4)
    guard let payload = Data(base64Encoded: encoded),
      let claims = try? JSONDecoder().decode(Claims.self, from: payload),
      claims.uid == session.user.id, claims.sub == session.user.id, !claims.sid.isEmpty,
      claims.iat >= 0, claims.iat <= Int(now().timeIntervalSince1970), claims.exp > Int(now().timeIntervalSince1970),
      claims.exp > claims.iat, claims.exp - claims.iat <= 3600
    else { throw NativeAuthFailure.invalidResponse }
    // Cache lifetime/owner checks are not cryptographic verification. Every API
    // and realtime service validates JWT signature and the live session itself.
    guard generation == cacheGeneration else { throw NativeAuthFailure.superseded }
    access = (session.token, token, Date(timeIntervalSince1970: TimeInterval(claims.exp)))
    return token
  }
}
