import XCTest

@testable import Omi_Computer

final class SelfHostedSpeechModelPolicyTests: XCTestCase {
  func testLocalTranscriptionFailsThroughExistingFallbackBeforeModelDownload() async {
    let previous = ProcessInfo.processInfo.environment["OMI_DEPLOYMENT_PROFILE"]
    setenv("OMI_DEPLOYMENT_PROFILE", "self_hosted", 1)
    defer {
      if let previous {
        setenv("OMI_DEPLOYMENT_PROFILE", previous, 1)
      } else {
        unsetenv("OMI_DEPLOYMENT_PROFILE")
      }
    }

    let fallback = expectation(description: "self-hosted local STT uses its existing fallback")
    let service = LocalTranscriptionService()
    service.start(onSegments: { _ in }, onModelLoadFailed: { fallback.fulfill() })

    await fulfillment(of: [fallback], timeout: 1)
  }

  func testSelfHostedPttLanguageIdentificationDoesNotLoadAnImplicitModel() async {
    let previous = ProcessInfo.processInfo.environment["OMI_DEPLOYMENT_PROFILE"]
    setenv("OMI_DEPLOYMENT_PROFILE", "self_hosted", 1)
    defer {
      if let previous {
        setenv("OMI_DEPLOYMENT_PROFILE", previous, 1)
      } else {
        unsetenv("OMI_DEPLOYMENT_PROFILE")
      }
    }

    let pcm = Data(repeating: 0, count: 16_000 * 2)
    let verdict = await PTTLanguageIdentifier.shared.identify(
      pcm16k: pcm, candidates: ["en"])

    XCTAssertNil(verdict.languageCode)
    XCTAssertNil(verdict.transcript)
  }
}
