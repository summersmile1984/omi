import Foundation
import XCTest

@testable import Omi_Computer

private final class CalendarWorkerURLProtocol: URLProtocol, @unchecked Sendable {
  nonisolated(unsafe) static var requests: [URLRequest] = []
  nonisolated(unsafe) static var responseBody = Data("[]".utf8)
  nonisolated(unsafe) static var responseStatus = 200

  override class func canInit(with request: URLRequest) -> Bool { true }

  override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

  override func startLoading() {
    Self.requests.append(request)
    let response = HTTPURLResponse(
      url: request.url!,
      statusCode: Self.responseStatus,
      httpVersion: nil,
      headerFields: ["Content-Type": "application/json"]
    )!
    client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
    client?.urlProtocol(self, didLoad: Self.responseBody)
    client?.urlProtocolDidFinishLoading(self)
  }

  override func stopLoading() {}
}

final class CalendarWorkerAPIClientTests: XCTestCase {
  override func setUp() {
    super.setUp()
    CalendarWorkerURLProtocol.requests = []
    CalendarWorkerURLProtocol.responseStatus = 200
    CalendarWorkerURLProtocol.responseBody = Data(
      #"[{"event_id":"event-1","title":"Workers review","attendees":["Guest"],"attendee_emails":["guest@example.test"],"start_time":"2026-09-01T01:00:00.000Z","end_time":"2026-09-01T02:00:00.000Z","html_link":null}]"#
        .utf8
    )
  }

  func testListEventsUsesWorkerRouteAndClampsProviderLimit() async throws {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [CalendarWorkerURLProtocol.self]
    let client = APIClient(session: URLSession(configuration: configuration))
    await client.setTestAuthHeader("Bearer calendar-test")

    let events = try await client.listGoogleCalendarEvents(
      daysBack: 2,
      daysForward: 3,
      maxResults: 500
    )

    XCTAssertEqual(events.map(\.eventID), ["event-1"])
    guard let request = CalendarWorkerURLProtocol.requests.first,
      let url = request.url
    else {
      return XCTFail("expected a Worker request")
    }
    XCTAssertEqual(request.httpMethod, "GET")
    XCTAssertEqual(url.path, "/v1/calendar/google/events")
    XCTAssertEqual(url.query?.contains("max_results=100"), true)
    XCTAssertNotNil(url.query?.range(of: "time_min="))
    XCTAssertNotNil(url.query?.range(of: "time_max="))
    XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer calendar-test")
  }

  func testOAuthURLCarriesBundleDeepLinkWithoutLeakingItIntoPath() async throws {
    CalendarWorkerURLProtocol.responseBody = Data(
      #"{"auth_url":"https://accounts.google.com/o/oauth2/v2/auth?state=test"}"#.utf8
    )
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [CalendarWorkerURLProtocol.self]
    let client = APIClient(session: URLSession(configuration: configuration))
    await client.setTestAuthHeader("Bearer calendar-test")

    _ = try await client.googleCalendarOAuthURL(
      successRedirectURL: "omi-computer-dev://google_calendar/callback"
    )

    guard let url = CalendarWorkerURLProtocol.requests.first?.url else {
      return XCTFail("expected an OAuth URL request")
    }
    XCTAssertEqual(url.path, "/v1/integrations/google_calendar/oauth-url")
    XCTAssertEqual(
      URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first?.value,
      "omi-computer-dev://google_calendar/callback"
    )
  }
}
