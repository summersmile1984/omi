import Foundation

// The compiler inserts the real staged endpoint, upstream generic POST/GET,
// transport initialization and wire models. Only auth and response delivery are
// controlled here. The ordinary test lane never opens a network connection.
struct RuntimeOwnerAuthorizationSnapshot {}
struct AuthPolicy {
  var allowsAuthRetry = true
  var expectedAuthOwnerId: String?
}
enum APIError: Error {
  case invalidResponse
  case httpError(Int)
}
func log(_ message: String) {}

struct OmiHTTPTransport {
  let session: URLSession
  let encoder: JSONEncoder
  let decoder: JSONDecoder
  static let isoFractional = ISO8601DateFormatter()
  static let isoStandard = ISO8601DateFormatter()
  /* ACTUAL_TRANSPORT_METHODS */
}

final class APIClient {
  let transport = OmiHTTPTransport()
  var baseURL = "https://synthetic.invalid/"
  var requests: [URLRequest] = []
  var response = Data()
  var duration: TimeInterval = 0
  var status = 200
  var live = false

  func resolvedRequestAuthPolicy(
    expectedOwnerId: String?, authorizationSnapshot: RuntimeOwnerAuthorizationSnapshot?
  ) throws -> AuthPolicy { AuthPolicy(expectedAuthOwnerId: expectedOwnerId) }
  func validateExpectedOwner(_ policy: AuthPolicy) throws {}
  func buildHeaders(requireAuth: Bool, includeBYOK: Bool, expectedAuthOwnerId: String?) async throws -> [String: String]
  {
    ["Authorization": "Bearer synthetic-request-probe", "Content-Type": "application/json"]
  }
  func performRequest<T: Decodable>(_ request: URLRequest, authPolicy: AuthPolicy) async throws -> T {
    requests.append(request)
    let data: Data
    if live {
      let (received, result) = try await transport.session.data(for: request)
      guard let http = result as? HTTPURLResponse else { throw APIError.invalidResponse }
      guard http.statusCode == 200 else { throw APIError.httpError(http.statusCode) }
      data = received
    } else {
      guard request.timeoutInterval >= duration else { throw URLError(.timedOut) }
      guard status == 200 else { throw APIError.httpError(status) }
      data = response
    }
    return try transport.decoder.decode(T.self, from: data)
  }
  /* ACTUAL_GENERIC_POST */
  /* ACTUAL_GENERIC_GET */
  /* ACTUAL_STAGED_ADJUDICATION */
  /* ACTUAL_SETTINGS */
}

/* ACTUAL_WIRE_MODELS */

@main struct Probe {
  static func submission(_ count: Int) -> ScreenFrameAdjudicationRequestWire {
    ScreenFrameAdjudicationRequestWire(
      subjectID: "synthetic-meeting",
      candidates: (0..<count).map { index in
        ScreenFrameCandidateWire(
          clientFrameID: String(index), capturedAt: Date(timeIntervalSince1970: 100 + Double(index)),
          mimeType: "image/png", declaredWidth: 2, declaredHeight: 2,
          sha256Base64: "digest-\(index)", bytesBase64: Data([UInt8(index)]).base64EncodedString())
      })
  }

  static func main() async throws {
    let client = APIClient()
    if CommandLine.arguments.count == 2 {
      // Optional operator-run loopback transport measurement, separate from CI.
      let origin = URL(string: CommandLine.arguments[1])!
      precondition(origin.scheme == "http" && origin.host == "127.0.0.1")
      client.baseURL = origin.absoluteString
      client.live = true
      let request = submission(2)
      var start = Date()
      do {
        let _: ScreenFrameAdjudicationResponseWire = try await client.post(
          "v1/screen-frame-egress/adjudications", body: request)
        preconditionFailure("The original shared deadline should expire before the delayed response")
      } catch let error as URLError {
        precondition(error.code == .timedOut)
        print("original_timeout_seconds=\(Date().timeIntervalSince(start))")
      }
      start = Date()
      let result = try await client.adjudicateScreenFrames(request)
      precondition(result.attemptID == request.attemptID)
      precondition(result.frameSet.adjudicatedAt != nil)
      print("corrected_response_seconds=\(Date().timeIntervalSince(start))")
      print("loopback transport passed; model, auth and UI are not exercised")
      return
    }

    for count in [1, 8] {
      let request = submission(count)
      client.requests = []
      client.duration = 90 * Double(count)
      client.response = Data(
        """
        {"attempt_id":"\(request.attemptID)","outcome":"no_approved_frames",
         "frame_set":{"revision":0,"banner":null,"strip":[],"adjudicated_at":"2026-09-08T00:00:00Z"}}
        """.utf8)
      // This is precisely the original endpoint's generic POST call. The same
      // deterministic slow response must fail before the staged endpoint runs.
      do {
        let _: ScreenFrameAdjudicationResponseWire = try await client.post(
          "v1/screen-frame-egress/adjudications", body: request)
        preconditionFailure("Negative control unexpectedly survived the ordinary timeout")
      } catch let error as URLError { precondition(error.code == .timedOut) }
      client.requests = []
      let result = try await client.adjudicateScreenFrames(request)
      precondition(result.attemptID == request.attemptID && result.outcome == "no_approved_frames")
      precondition(result.frameSet.adjudicatedAt != nil)
      precondition(client.requests.count == 1)
      let captured = client.requests[0]
      precondition(captured.url?.path == "/v1/screen-frame-egress/adjudications")
      precondition(captured.httpMethod == "POST")
      precondition(captured.timeoutInterval == (count == 1 ? 240 : 1500))
      precondition(captured.value(forHTTPHeaderField: "Authorization") == "Bearer synthetic-request-probe")
      let expectedBody = try client.transport.encoder.encode(request)
      let expectedJSON = try JSONSerialization.jsonObject(with: expectedBody) as! NSDictionary
      let actualJSON = try JSONSerialization.jsonObject(with: captured.httpBody!) as! NSDictionary
      precondition(actualJSON == expectedJSON)
    }

    client.requests = []
    client.status = 503
    client.duration = 80
    do {
      let _ = try await client.adjudicateScreenFrames(submission(2))
      preconditionFailure("Writer failure must propagate")
    } catch APIError.httpError(let status) { precondition(status == 503) }
    precondition(client.requests.count == 1)

    client.requests = []
    client.status = 200
    client.duration = 0
    client.response = Data("{\"meeting_note_screenshots_enabled\":true}".utf8)
    let settings = try await client.getScreenFrameSettings()
    precondition(settings.meetingNoteScreenshotsEnabled)
    let settingsRequest = client.requests[0]
    precondition(settingsRequest.timeoutInterval == URLRequest(url: settingsRequest.url!).timeoutInterval)
    precondition(client.transport.session.configuration.timeoutIntervalForRequest == 30)
    print("screen frame request passed: negative controls, 1/8 candidates, exact bytes, failure, ordinary settings")
  }
}
