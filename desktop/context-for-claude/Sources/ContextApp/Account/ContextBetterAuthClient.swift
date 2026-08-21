import Foundation

struct ContextBetterAuthCredential: Equatable, Sendable {
  let jwt: String
  let sessionToken: String
  let expiresIn: Int
  let userID: String
  let email: String?
}

enum ContextBetterAuthError: LocalizedError, Equatable {
  case invalidResponse
  case credentialsRejected
  case sessionRejected
  case http(Int)

  var errorDescription: String? {
    switch self {
    case .invalidResponse: return "The authentication service returned an unreadable response."
    case .credentialsRejected: return "The email or password was not accepted."
    case .sessionRejected: return "Your self-hosted session expired. Sign in again."
    case .http(let status): return "The authentication service is unavailable (HTTP \(status))."
    }
  }
}

/// Better Auth keeps the revocable session. Context stores its opaque token in
/// Keychain and exchanges it for the short-lived JWT sent to backend APIs.
struct ContextBetterAuthClient: Sendable {
  typealias Transport = @Sendable (URLRequest) async throws -> (Data, URLResponse)

  let baseURL: URL
  let transport: Transport

  init(baseURL: URL, transport: @escaping Transport = { try await URLSession.shared.data(for: $0) }) {
    self.baseURL = baseURL
    self.transport = transport
  }

  func signIn(email: String, password: String) async throws -> ContextBetterAuthCredential {
    try await authenticate(path: "api/auth/sign-in/email", body: ["email": email, "password": password])
  }

  func signUp(name: String, email: String, password: String) async throws -> ContextBetterAuthCredential {
    try await authenticate(
      path: "api/auth/sign-up/email", body: ["name": name, "email": email, "password": password])
  }

  func refresh(sessionToken: String, expectedUserID: String?) async throws -> ContextBetterAuthCredential {
    let session = try await sessionEnvelope(sessionToken: sessionToken)
    if let expectedUserID, !expectedUserID.isEmpty, session.user.id != expectedUserID {
      throw ContextBetterAuthError.sessionRejected
    }
    return try await credential(sessionToken: sessionToken, user: session.user)
  }

  func signOut(sessionToken: String) async throws {
    var request = makeRequest(path: "api/auth/sign-out", method: "POST")
    request.setValue("Bearer \(sessionToken)", forHTTPHeaderField: "Authorization")
    request.httpBody = Data("{}".utf8)
    let (_, response) = try await transport(request)
    try requireSuccess(response, rejected: .sessionRejected)
  }

  private func authenticate(path: String, body: [String: String]) async throws -> ContextBetterAuthCredential {
    var request = makeRequest(path: path, method: "POST")
    request.httpBody = try JSONSerialization.data(withJSONObject: body)
    let (data, response) = try await transport(request)
    try requireSuccess(response, rejected: .credentialsRejected)
    let envelope = try JSONDecoder().decode(SignInEnvelope.self, from: data)
    guard
      let http = response as? HTTPURLResponse,
      let sessionToken = http.value(forHTTPHeaderField: "set-auth-token"),
      !sessionToken.isEmpty,
      !envelope.user.id.isEmpty
    else { throw ContextBetterAuthError.invalidResponse }
    return try await credential(sessionToken: sessionToken, user: envelope.user)
  }

  private func credential(sessionToken: String, user: UserEnvelope) async throws -> ContextBetterAuthCredential {
    var request = makeRequest(path: "api/auth/token", method: "GET")
    request.setValue("Bearer \(sessionToken)", forHTTPHeaderField: "Authorization")
    let (data, response) = try await transport(request)
    try requireSuccess(response, rejected: .sessionRejected)
    let envelope = try JSONDecoder().decode(JWTEnvelope.self, from: data)
    let owner = Self.subject(from: envelope.token)
    guard !envelope.token.isEmpty, owner == user.id else { throw ContextBetterAuthError.invalidResponse }
    return ContextBetterAuthCredential(
      jwt: envelope.token,
      sessionToken: sessionToken,
      expiresIn: Self.remainingLifetime(of: envelope.token),
      userID: user.id,
      email: user.email)
  }

  private func sessionEnvelope(sessionToken: String) async throws -> SessionEnvelope {
    var request = makeRequest(path: "api/auth/get-session", method: "GET")
    request.setValue("Bearer \(sessionToken)", forHTTPHeaderField: "Authorization")
    let (data, response) = try await transport(request)
    try requireSuccess(response, rejected: .sessionRejected)
    guard !data.isEmpty, String(data: data, encoding: .utf8) != "null" else {
      throw ContextBetterAuthError.sessionRejected
    }
    return try JSONDecoder().decode(SessionEnvelope.self, from: data)
  }

  private func makeRequest(path: String, method: String) -> URLRequest {
    var request = URLRequest(url: baseURL.appendingPathComponent(path))
    request.httpMethod = method
    request.timeoutInterval = 15
    request.setValue("application/json", forHTTPHeaderField: "Accept")
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    return request
  }

  private func requireSuccess(_ response: URLResponse, rejected: ContextBetterAuthError) throws {
    guard let http = response as? HTTPURLResponse else { throw ContextBetterAuthError.invalidResponse }
    guard (200..<300).contains(http.statusCode) else {
      if [400, 401, 403].contains(http.statusCode) { throw rejected }
      throw ContextBetterAuthError.http(http.statusCode)
    }
  }

  static func subject(from token: String) -> String? {
    payload(from: token)?["uid"] as? String ?? payload(from: token)?["sub"] as? String
  }

  static func remainingLifetime(of token: String, now: Date = Date()) -> Int {
    let expiration = (payload(from: token)?["exp"] as? NSNumber)?.doubleValue
    return max(1, Int((expiration ?? now.timeIntervalSince1970 + 900) - now.timeIntervalSince1970))
  }

  private static func payload(from token: String) -> [String: Any]? {
    let parts = token.split(separator: ".", omittingEmptySubsequences: false)
    guard parts.count == 3 else { return nil }
    var encoded = String(parts[1]).replacingOccurrences(of: "-", with: "+")
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
  private struct SessionEnvelope: Decodable { let user: UserEnvelope }
  private struct JWTEnvelope: Decodable { let token: String }
}
