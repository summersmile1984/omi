import Foundation
import XCTest

@testable import Omi_Computer

final class BetterAuthClientTests: XCTestCase {
  func testSignInExchangesRevocableSessionForBackendJWT() async throws {
    let jwt = makeJWT(subject: "better-user", expiresAt: Date().addingTimeInterval(900))
    let recorder = RequestRecorder(responses: [
      response(
        path: "/api/auth/sign-in/email",
        status: 200,
        body: #"{"token":"raw-database-session","user":{"id":"better-user","email":"user@example.com"}}"#,
        headers: ["set-auth-token": "session-secret"]),
      response(path: "/api/auth/token", status: 200, body: #"{"token":"\#(jwt)"}"#),
    ])
    let client = BetterAuthClient(baseURL: URL(string: "https://auth.self-hosted.example/")!) {
      try await recorder.send($0)
    }

    let credential = try await client.signIn(email: "user@example.com", password: "correct horse battery staple")

    XCTAssertEqual(credential.userID, "better-user")
    XCTAssertEqual(credential.email, "user@example.com")
    XCTAssertEqual(credential.sessionToken, "session-secret")
    XCTAssertEqual(credential.jwt, jwt)
    let requests = await recorder.requests
    XCTAssertEqual(requests.map(\.url?.path), ["/api/auth/sign-in/email", "/api/auth/token"])
    XCTAssertNil(requests[0].value(forHTTPHeaderField: "Authorization"))
    XCTAssertEqual(requests[1].value(forHTTPHeaderField: "Authorization"), "Bearer session-secret")
  }

  func testRefreshValidatesSessionOwnerBeforeMintingJWT() async throws {
    let recorder = RequestRecorder(responses: [
      response(
        path: "/api/auth/get-session",
        status: 200,
        body: #"{"session":{"id":"s1"},"user":{"id":"different-user","email":"other@example.com"}}"#)
    ])
    let client = BetterAuthClient(baseURL: URL(string: "https://auth.self-hosted.example/")!) {
      try await recorder.send($0)
    }

    do {
      _ = try await client.refresh(sessionToken: "session-secret", expectedUserID: "expected-user")
      XCTFail("expected owner mismatch to reject the session")
    } catch {
      XCTAssertEqual(error as? BetterAuthClientError, .sessionRejected)
    }
    let requests = await recorder.requests
    XCTAssertEqual(requests.count, 1)
  }

  func testCredentialRejectionDoesNotAttemptJWTMint() async throws {
    let recorder = RequestRecorder(responses: [
      response(path: "/api/auth/sign-in/email", status: 401, body: #"{"message":"invalid"}"#)
    ])
    let client = BetterAuthClient(baseURL: URL(string: "https://auth.self-hosted.example/")!) {
      try await recorder.send($0)
    }

    do {
      _ = try await client.signIn(email: "user@example.com", password: "wrong")
      XCTFail("expected credentials to be rejected")
    } catch {
      XCTAssertEqual(error as? BetterAuthClientError, .credentialsRejected)
    }
    let requests = await recorder.requests
    XCTAssertEqual(requests.count, 1)
  }

  func testRawBodyTokenIsNeverPersistedWithoutSignedBearerHeader() async throws {
    let recorder = RequestRecorder(responses: [
      response(
        path: "/api/auth/sign-in/email",
        status: 200,
        body: #"{"token":"raw-database-session","user":{"id":"better-user","email":null}}"#)
    ])
    let client = BetterAuthClient(baseURL: URL(string: "https://auth.self-hosted.example/")!) {
      try await recorder.send($0)
    }

    do {
      _ = try await client.signIn(email: "user@example.com", password: "password")
      XCTFail("expected unsigned body token to be rejected")
    } catch {
      XCTAssertEqual(error as? BetterAuthClientError, .invalidResponse)
    }
    let requests = await recorder.requests
    XCTAssertEqual(requests.count, 1)
  }

  private func makeJWT(subject: String, expiresAt: Date) -> String {
    func encoded(_ object: [String: Any]) -> String {
      let data = try! JSONSerialization.data(withJSONObject: object)
      return data.base64EncodedString()
        .replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
    }
    return "\(encoded(["alg": "ES256", "typ": "JWT"]))"
      + ".\(encoded(["sub": subject, "uid": subject, "exp": expiresAt.timeIntervalSince1970])).signature"
  }

  private func response(
    path: String, status: Int, body: String, headers: [String: String] = [:]
  ) -> RecordedResponse {
    RecordedResponse(path: path, status: status, data: Data(body.utf8), headers: headers)
  }
}

private struct RecordedResponse: Sendable {
  let path: String
  let status: Int
  let data: Data
  let headers: [String: String]
}

private actor RequestRecorder {
  private var remaining: [RecordedResponse]
  private(set) var requests: [URLRequest] = []

  init(responses: [RecordedResponse]) {
    remaining = responses
  }

  func send(_ request: URLRequest) throws -> (Data, URLResponse) {
    requests.append(request)
    guard !remaining.isEmpty else { throw URLError(.badServerResponse) }
    let next = remaining.removeFirst()
    guard request.url?.path == next.path else { throw URLError(.badURL) }
    let response = HTTPURLResponse(
      url: request.url!, statusCode: next.status, httpVersion: "HTTP/1.1", headerFields: next.headers)!
    return (next.data, response)
  }
}
