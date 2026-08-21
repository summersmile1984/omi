import Foundation
import XCTest

@testable import ContextApp

final class ContextBetterAuthClientTests: XCTestCase {
  func testSignInStoresOpaqueSessionAndMintsJWT() async throws {
    let jwt = makeJWT(subject: "user-1")
    let recorder = ContextRequestRecorder(responses: [
      response(
        path: "/api/auth/sign-in/email", status: 200,
        body: #"{"token":"raw-database-session","user":{"id":"user-1","email":"user@example.com"}}"#,
        headers: ["set-auth-token": "session-secret"]),
      response(path: "/api/auth/token", status: 200, body: "{\"token\":\"\(jwt)\"}"),
    ])
    let client = ContextBetterAuthClient(baseURL: URL(string: "https://auth.example.com/")!) {
      try await recorder.send($0)
    }

    let result = try await client.signIn(email: "user@example.com", password: "correct horse battery staple")
    let requests = await recorder.requests
    XCTAssertEqual(result.sessionToken, "session-secret")
    XCTAssertEqual(result.jwt, jwt)
    XCTAssertEqual(requests.map { $0.url?.path }, ["/api/auth/sign-in/email", "/api/auth/token"])
    XCTAssertEqual(requests.last?.value(forHTTPHeaderField: "Authorization"), "Bearer session-secret")
  }

  func testRefreshRejectsOwnerMismatchBeforeMintingJWT() async throws {
    let recorder = ContextRequestRecorder(responses: [
      response(path: "/api/auth/get-session", status: 200, body: #"{"user":{"id":"other","email":null}}"#)
    ])
    let client = ContextBetterAuthClient(baseURL: URL(string: "https://auth.example.com/")!) {
      try await recorder.send($0)
    }

    do {
      _ = try await client.refresh(sessionToken: "session-secret", expectedUserID: "expected")
      XCTFail("expected rejection")
    } catch {
      XCTAssertEqual(error as? ContextBetterAuthError, .sessionRejected)
    }
    let requests = await recorder.requests
    XCTAssertEqual(requests.count, 1)
  }

  private func makeJWT(subject: String) -> String {
    func part(_ object: [String: Any]) -> String {
      let data = try! JSONSerialization.data(withJSONObject: object)
      return data.base64EncodedString().replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    }
    let header = part(["alg": "RS256"])
    let payload = part(["sub": subject, "exp": Date().timeIntervalSince1970 + 900])
    return "\(header).\(payload).signature"
  }

  private func response(
    path: String, status: Int, body: String, headers: [String: String] = [:]
  ) -> ContextRecordedResponse {
    ContextRecordedResponse(
      path: path,
      data: Data(body.utf8),
      response: HTTPURLResponse(
        url: URL(string: "https://auth.example.com\(path)")!,
        statusCode: status,
        httpVersion: nil,
        headerFields: headers)!)
  }
}

private struct ContextRecordedResponse: Sendable {
  let path: String
  let data: Data
  let response: HTTPURLResponse
}

private actor ContextRequestRecorder {
  private(set) var requests: [URLRequest] = []
  private var responses: [ContextRecordedResponse]

  init(responses: [ContextRecordedResponse]) { self.responses = responses }

  func send(_ request: URLRequest) throws -> (Data, URLResponse) {
    requests.append(request)
    guard !responses.isEmpty else { throw URLError(.badServerResponse) }
    let next = responses.removeFirst()
    XCTAssertEqual(request.url?.path, next.path)
    return (next.data, next.response)
  }
}
