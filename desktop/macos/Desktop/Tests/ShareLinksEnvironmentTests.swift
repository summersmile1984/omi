import XCTest

@testable import Omi_Computer

final class ShareLinksEnvironmentTests: XCTestCase {
  func testShareBaseURLDefaultsToProduction() {
    XCTAssertEqual(
      DesktopBackendEnvironment.shareBaseURL(environmentValue: nil),
      "https://h.omi.me"
    )
    XCTAssertEqual(
      DesktopBackendEnvironment.conversationShareURL(id: "abc", environmentValue: nil),
      "https://h.omi.me/conversations/abc"
    )
  }

  func testShareBaseURLHonorsOverride() {
    XCTAssertEqual(
      DesktopBackendEnvironment.shareBaseURL(environmentValue: "https://share.example.com/"),
      "https://share.example.com"
    )
    XCTAssertEqual(
      DesktopBackendEnvironment.shareBaseURL(environmentValue: "share.example.com"),
      "https://share.example.com"
    )
    XCTAssertEqual(
      DesktopBackendEnvironment.conversationShareURL(
        id: "abc",
        environmentValue: "https://share.example.com"
      ),
      "https://share.example.com/conversations/abc"
    )
  }

  func testShareBaseURLFallsBackForMalformedOverride() {
    XCTAssertEqual(
      DesktopBackendEnvironment.shareBaseURL(environmentValue: "not a url"),
      "https://h.omi.me"
    )
    XCTAssertEqual(
      DesktopBackendEnvironment.shareBaseURL(environmentValue: "ftp://share.example.com"),
      "https://h.omi.me"
    )
  }

  func testSelfHostedShareDefaultsToConfiguredBackendInsteadOfOmi() {
    XCTAssertEqual(
      DesktopBackendEnvironment.shareBaseURL(
        environmentValue: nil,
        deploymentProfile: .selfHosted,
        selfHostedBackendURL: "https://backend.fork.example/"),
      "https://backend.fork.example"
    )
    XCTAssertEqual(
      DesktopBackendEnvironment.conversationShareURL(
        id: "abc",
        environmentValue: nil,
        deploymentProfile: .selfHosted,
        selfHostedBackendURL: "https://backend.fork.example/"),
      "https://backend.fork.example/conversations/abc"
    )
  }
}
