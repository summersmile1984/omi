import Foundation
import XCTest

@testable import Omi_Computer

final class OnboardingWebResearchServiceTests: XCTestCase {
  func testSelfHostedSearchDoesNotInvokeDirectWebLoader() async {
    let service = OnboardingWebResearchService(
      deploymentProfile: .selfHosted,
      dataLoader: { _ in
        fatalError("self-hosted onboarding must not construct direct web requests")
      }
    )

    let results = await service.search(queries: ["profile-derived private query"])

    XCTAssertTrue(results.isEmpty)
  }

  func testCloudSearchUsesInjectedLoaderAndParsesResults() async {
    let service = OnboardingWebResearchService(
      deploymentProfile: .omiCloud,
      dataLoader: { request in
        let html = """
          <a class="result__a" href="https://example.com/result">Example result</a>
          <div class="result__snippet">A bounded public snippet.</div>
          """
        guard let url = request.url else {
          XCTFail("search request must contain a URL")
          return (Data(), URLResponse())
        }
        guard
          let response = HTTPURLResponse(
            url: url, statusCode: 200, httpVersion: nil, headerFields: nil
          )
        else {
          XCTFail("HTTP response construction must succeed")
          return (Data(), URLResponse())
        }
        return (Data(html.utf8), response)
      }
    )

    let results = await service.search(queries: ["public query"], maxResultsPerQuery: 1)

    XCTAssertEqual(results.count, 1)
    XCTAssertEqual(results[0].title, "Example result")
    XCTAssertEqual(results[0].url, "https://example.com/result")
    XCTAssertEqual(results[0].snippet, "A bounded public snippet.")
  }
}
