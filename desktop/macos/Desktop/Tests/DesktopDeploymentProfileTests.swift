import XCTest

@testable import Omi_Computer

final class DesktopDeploymentProfileTests: XCTestCase {
  func testProfileResolutionFailsClosedForUnknownValues() {
    XCTAssertEqual(DesktopDeploymentProfile.resolve(nil), .omiCloud)
    XCTAssertEqual(DesktopDeploymentProfile.resolve(" self_hosted "), .selfHosted)
    XCTAssertEqual(DesktopDeploymentProfile.resolve("unexpected"), .selfHosted)
  }

  func testLegacyDirectNotionExportIsUnavailableForSelfHostedProfile() async {
    let service = MemoryExportService(deploymentProfile: .selfHosted)
    do {
      _ = try await service.exportToNotion(token: "stale-token", parentPageID: "stale-page")
      XCTFail("self-hosted profile must reject the legacy direct Notion path")
    } catch let error as MemoryExportError {
      XCTAssertEqual(
        error.errorDescription,
        "This Notion export path is unavailable in the self-hosted deployment. Configure an operator-owned connector instead."
      )
    } catch {
      XCTFail("unexpected error: \(error)")
    }
  }

  func testSelfHostedPolicyDisablesClientVendorAndManagedServices() {
    XCTAssertEqual(
      DesktopBackendEnvironment.identityProvider(
        deploymentProfile: .selfHosted,
        configuredValue: nil
      ),
      .betterAuth
    )
    XCTAssertFalse(DesktopBackendEnvironment.shouldConfigureFirebaseSDK(identityProvider: .betterAuth))
    XCTAssertFalse(
      DesktopModelEgressPolicy.allowsClientDirectVendorEgress(deploymentProfile: .selfHosted))
    XCTAssertFalse(
      DesktopModelEgressPolicy.allowsClientDirectWebSearch(deploymentProfile: .selfHosted))
    XCTAssertFalse(DesktopModelEgressPolicy.allowsBYOK(deploymentProfile: .selfHosted))
    XCTAssertFalse(DesktopBackendEnvironment.allowsOmiManagedServices(deploymentProfile: .selfHosted))
    XCTAssertTrue(DesktopModelEgressPolicy.allowsClientDirectVendorEgress(deploymentProfile: .omiCloud))
    XCTAssertTrue(DesktopModelEgressPolicy.allowsClientDirectWebSearch(deploymentProfile: .omiCloud))
    XCTAssertFalse(
      DesktopModelEgressPolicy.allowsGoogleBrowserConnectors(deploymentProfile: .selfHosted))
    XCTAssertTrue(
      DesktopModelEgressPolicy.allowsGoogleBrowserConnectors(deploymentProfile: .omiCloud))
    XCTAssertFalse(APIKeyService.allowsBackendKeyFetch(deploymentProfile: .selfHosted))
    XCTAssertTrue(APIKeyService.allowsBackendKeyFetch(deploymentProfile: .omiCloud))
  }

  func testSelfHostedBackendRoutesRequireAnExplicitCanonicalOperatorOrigin() {
    XCTAssertEqual(
      DesktopBackendEnvironment.pythonBaseURL(
        useDevelopmentBackends: false,
        bundleIdentifier: AppBuild.productionBundleIdentifier,
        environmentValue: "https://operator.example/",
        deploymentProfile: .selfHosted
      ),
      "https://operator.example/"
    )
    XCTAssertEqual(
      DesktopBackendEnvironment.pythonBaseURL(
        useDevelopmentBackends: false,
        bundleIdentifier: AppBuild.productionBundleIdentifier,
        environmentValue: "http://operator.example/",
        deploymentProfile: .selfHosted
      ),
      ""
    )
    XCTAssertEqual(
      DesktopBackendEnvironment.pythonBaseURL(
        useDevelopmentBackends: false,
        bundleIdentifier: AppBuild.productionBundleIdentifier,
        environmentValue: "https://api.omi.me/",
        deploymentProfile: .selfHosted
      ),
      ""
    )
  }

  func testCanonicalOriginRejectsPathCredentialsAndQuery() throws {
    XCTAssertEqual(
      try DesktopBackendEnvironment.canonicalSelfHostedOrigin(
        "https://Operator.Example:443",
        key: "OMI_PYTHON_API_URL",
        requiresHTTPS: true
      ),
      "https://operator.example/"
    )
    for value in [
      "https://operator.example/api",
      "https://user:pass@operator.example",
      "https://operator.example?tenant=one",
      "https://api.omi.me",
      "http://operator.example",
    ] {
      XCTAssertThrowsError(
        try DesktopBackendEnvironment.canonicalSelfHostedOrigin(
          value,
          key: "OMI_PYTHON_API_URL",
          requiresHTTPS: true
        )
      )
    }
  }

  func testSelfHostedUpdatePathsNeverFallBackToOmiManagedDownload() {
    XCTAssertFalse(AppBuild.allowsSparkleUpdates(deploymentProfile: .selfHosted))
    XCTAssertEqual(
      AppBuild.manualDownloadURL(
        channel: "stable",
        isBetaIdentity: false,
        deploymentProfile: .selfHosted,
        backendBaseURL: "https://operator.example/"
      ).absoluteString,
      "https://operator.example/v2/desktop/download/latest?channel=stable"
    )
    XCTAssertEqual(
      AppBuild.manualDownloadURL(
        channel: "stable",
        isBetaIdentity: false,
        deploymentProfile: .selfHosted,
        backendBaseURL: nil
      ).path,
      "/"
    )
    XCTAssertEqual(
      AppBuild.manualDownloadURL(
        channel: "stable",
        isBetaIdentity: false,
        deploymentProfile: .selfHosted,
        backendBaseURL: "https://api.omi.me/"
      ).path,
      "/"
    )
  }

  func testSelfHostedUpdateAuthorityRequiresOperatorFeedAndBakedSparkleKey() {
    let publicKey = Data(repeating: 7, count: 32).base64EncodedString()
    let authority = AppBuild.resolveUpdateAuthority(
      deploymentProfile: .selfHosted,
      feedURLValue: "https://updates.operator.example/macos/appcast.xml",
      publicKeyValue: publicKey,
      bundleIdentifier: "com.omi.computer-macos"
    )
    XCTAssertEqual(
      authority,
      .operatorOwned(URL(string: "https://updates.operator.example/macos/appcast.xml")!)
    )
    XCTAssertTrue(
      AppBuild.allowsSparkleUpdates(deploymentProfile: .selfHosted, authority: authority))

    XCTAssertEqual(
      AppBuild.resolveUpdateAuthority(
        deploymentProfile: .selfHosted,
        feedURLValue: nil,
        publicKeyValue: publicKey,
        bundleIdentifier: "com.omi.computer-macos"
      ),
      .unavailable(.missingFeedURL)
    )
    XCTAssertEqual(
      AppBuild.resolveUpdateAuthority(
        deploymentProfile: .selfHosted,
        feedURLValue: "https://updates.operator.example/macos/appcast.xml",
        publicKeyValue: "not-a-key",
        bundleIdentifier: "com.omi.computer-macos"
      ),
      .unavailable(.missingPublicKey)
    )
    XCTAssertEqual(
      AppBuild.resolveUpdateAuthority(
        deploymentProfile: .selfHosted,
        feedURLValue: "https://api.omi.me/v2/desktop/appcast.xml",
        publicKeyValue: publicKey,
        bundleIdentifier: "com.omi.computer-macos"
      ),
      .unavailable(.invalidFeedURL)
    )
    XCTAssertEqual(
      AppBuild.resolveUpdateAuthority(
        deploymentProfile: .selfHosted,
        feedURLValue: "https://updates.operator.example/macos/appcast.xml?channel=stable",
        publicKeyValue: publicKey,
        bundleIdentifier: "com.omi.computer-macos"
      ),
      .unavailable(.invalidFeedURL)
    )
  }

  func testBYOKValidationFailsBeforeProviderRequestInSelfHostedProfile() async {
    let status = await BYOKValidator.validate(
      .openai,
      key: "not-a-real-key",
      deploymentProfile: .selfHosted
    )
    guard case .failed(let message) = status else {
      return XCTFail("self-hosted BYOK validation must fail closed")
    }
    XCTAssertTrue(message.contains("backend"))
  }

  @MainActor
  func testSelfHostedAuthUsesOperatorTokenAndDoesNotFallBackToFirebase() async throws {
    let oldProfile = getenv("OMI_DEPLOYMENT_PROFILE").flatMap { String(validatingCString: $0) }
    let oldToken = getenv("OMI_AUTH_API_TOKEN").flatMap { String(validatingCString: $0) }
    defer {
      if let oldProfile {
        setenv("OMI_DEPLOYMENT_PROFILE", oldProfile, 1)
      } else {
        unsetenv("OMI_DEPLOYMENT_PROFILE")
      }
      if let oldToken {
        setenv("OMI_AUTH_API_TOKEN", oldToken, 1)
      } else {
        unsetenv("OMI_AUTH_API_TOKEN")
      }
    }

    setenv("OMI_DEPLOYMENT_PROFILE", "self_hosted", 1)
    setenv("OMI_AUTH_API_TOKEN", "operator-jwt", 1)
    let token = try await AuthService.shared.getIdToken()
    XCTAssertEqual(token, "operator-jwt")

    unsetenv("OMI_AUTH_API_TOKEN")
    do {
      _ = try await AuthService.shared.getIdToken()
      XCTFail("self-hosted auth must not fall back to Firebase without an operator token")
    } catch AuthError.invalidConfiguration {
      // Expected fail-closed behavior.
    }
  }
}
