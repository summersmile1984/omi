import Foundation

struct BetterAuthCredential: Equatable, Sendable {
  let jwt: String
  let sessionToken: String
  let expiresIn: Int
  let userID: String
  let email: String?
}

enum BetterAuthClientError: LocalizedError, Equatable {
  case invalidConfiguration
  case invalidResponse
  case credentialsRejected
  case sessionRejected
  case http(Int)

  var errorDescription: String? {
    switch self {
    case .invalidConfiguration:
      return "This self-hosted build has an invalid authentication endpoint."
    case .invalidResponse:
      return "The authentication service returned an unreadable response."
    case .credentialsRejected:
      return "The email or password was not accepted."
    case .sessionRejected:
      return "Your self-hosted session expired. Sign in again."
    case .http(let status):
      return "The authentication service is unavailable (HTTP \(status))."
    }
  }
}

/// Native Better Auth wire client for a signed self-hosted deployment.
///
/// Better Auth owns the revocable database session. The desktop stores that
/// opaque session token in Keychain and exchanges it for a short-lived JWKS JWT
/// at `/api/auth/token`; the JWT, never the session token, is sent to Omi APIs.
struct BetterAuthClient: Sendable {
  typealias Transport = @Sendable (URLRequest) async throws -> (Data, URLResponse)

  let baseURL: URL
  var transport: Transport = { try await URLSession.shared.data(for: $0) }

  init(baseURL: URL, transport: @escaping Transport = { try await URLSession.shared.data(for: $0) }) {
    self.baseURL = baseURL
    self.transport = transport
  }

  func signIn(email: String, password: String) async throws -> BetterAuthCredential {
    try await authenticate(path: "api/auth/sign-in/email", body: ["email": email, "password": password])
  }

  func signUp(name: String, email: String, password: String) async throws -> BetterAuthCredential {
    try await authenticate(
      path: "api/auth/sign-up/email",
      body: ["name": name, "email": email, "password": password]
    )
  }

  func refresh(sessionToken: String, expectedUserID: String?) async throws -> BetterAuthCredential {
    let session = try await sessionEnvelope(sessionToken: sessionToken)
    if let expectedUserID, !expectedUserID.isEmpty, session.user.id != expectedUserID {
      throw BetterAuthClientError.sessionRejected
    }
    let jwt = try await jwt(sessionToken: sessionToken)
    let userID = Self.subject(from: jwt) ?? session.user.id
    guard userID == session.user.id else { throw BetterAuthClientError.invalidResponse }
    return BetterAuthCredential(
      jwt: jwt,
      sessionToken: sessionToken,
      expiresIn: Self.remainingLifetime(of: jwt),
      userID: userID,
      email: session.user.email
    )
  }

  func signOut(sessionToken: String) async throws {
    var request = try makeRequest(path: "api/auth/sign-out", method: "POST")
    request.setValue("Bearer \(sessionToken)", forHTTPHeaderField: "Authorization")
    request.httpBody = Data("{}".utf8)
    let (_, response) = try await transport(request)
    try requireSuccess(response, rejected: .sessionRejected)
  }

  private func authenticate(path: String, body: [String: String]) async throws -> BetterAuthCredential {
    var request = try makeRequest(path: path, method: "POST")
    request.httpBody = try JSONSerialization.data(withJSONObject: body)
    let (data, response) = try await transport(request)
    try requireSuccess(response, rejected: .credentialsRejected)
    let envelope = try JSONDecoder().decode(SignInEnvelope.self, from: data)
    guard
      let http = response as? HTTPURLResponse,
      let sessionToken = http.value(forHTTPHeaderField: "set-auth-token"),
      !sessionToken.isEmpty,
      !envelope.user.id.isEmpty
    else {
      throw BetterAuthClientError.invalidResponse
    }
    let jwt = try await jwt(sessionToken: sessionToken)
    let userID = Self.subject(from: jwt) ?? envelope.user.id
    guard userID == envelope.user.id else { throw BetterAuthClientError.invalidResponse }
    return BetterAuthCredential(
      jwt: jwt,
      sessionToken: sessionToken,
      expiresIn: Self.remainingLifetime(of: jwt),
      userID: userID,
      email: envelope.user.email
    )
  }

  private func sessionEnvelope(sessionToken: String) async throws -> SessionEnvelope {
    var request = try makeRequest(path: "api/auth/get-session", method: "GET")
    request.setValue("Bearer \(sessionToken)", forHTTPHeaderField: "Authorization")
    let (data, response) = try await transport(request)
    try requireSuccess(response, rejected: .sessionRejected)
    guard !data.isEmpty, String(data: data, encoding: .utf8) != "null" else {
      throw BetterAuthClientError.sessionRejected
    }
    return try JSONDecoder().decode(SessionEnvelope.self, from: data)
  }

  private func jwt(sessionToken: String) async throws -> String {
    var request = try makeRequest(path: "api/auth/token", method: "GET")
    request.setValue("Bearer \(sessionToken)", forHTTPHeaderField: "Authorization")
    let (data, response) = try await transport(request)
    try requireSuccess(response, rejected: .sessionRejected)
    let envelope = try JSONDecoder().decode(JWTEnvelope.self, from: data)
    guard !envelope.token.isEmpty, Self.subject(from: envelope.token) != nil else {
      throw BetterAuthClientError.invalidResponse
    }
    return envelope.token
  }

  private func makeRequest(path: String, method: String) throws -> URLRequest {
    guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
      throw BetterAuthClientError.invalidConfiguration
    }
    var request = URLRequest(url: url)
    request.httpMethod = method
    request.timeoutInterval = 15
    request.setValue("application/json", forHTTPHeaderField: "Accept")
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.setValue("omi-desktop/1.0", forHTTPHeaderField: "User-Agent")
    return request
  }

  private func requireSuccess(_ response: URLResponse, rejected: BetterAuthClientError) throws {
    guard let http = response as? HTTPURLResponse else { throw BetterAuthClientError.invalidResponse }
    guard (200..<300).contains(http.statusCode) else {
      if http.statusCode == 400 || http.statusCode == 401 || http.statusCode == 403 {
        throw rejected
      }
      throw BetterAuthClientError.http(http.statusCode)
    }
  }

  static func subject(from token: String) -> String? {
    stringClaim("uid", from: token) ?? stringClaim("sub", from: token)
  }

  static func remainingLifetime(of token: String, now: Date = Date()) -> Int {
    guard let expiration = numericClaim("exp", from: token) else { return 900 }
    return max(1, Int(expiration - now.timeIntervalSince1970))
  }

  private static func stringClaim(_ name: String, from token: String) -> String? {
    payload(from: token)?[name] as? String
  }

  private static func numericClaim(_ name: String, from token: String) -> Double? {
    if let value = payload(from: token)?[name] as? Double { return value }
    if let value = payload(from: token)?[name] as? Int { return Double(value) }
    return nil
  }

  private static func payload(from token: String) -> [String: Any]? {
    let parts = token.split(separator: ".", omittingEmptySubsequences: false)
    guard parts.count == 3 else { return nil }
    var encoded = String(parts[1])
      .replacingOccurrences(of: "-", with: "+")
      .replacingOccurrences(of: "_", with: "/")
    let remainder = encoded.count % 4
    if remainder != 0 { encoded.append(String(repeating: "=", count: 4 - remainder)) }
    guard let data = Data(base64Encoded: encoded) else { return nil }
    return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
  }

  private struct UserEnvelope: Decodable {
    let id: String
    let email: String?
  }

  private struct SignInEnvelope: Decodable {
    let user: UserEnvelope
  }

  private struct SessionEnvelope: Decodable {
    let user: UserEnvelope
  }

  private struct JWTEnvelope: Decodable {
    let token: String
  }
}
