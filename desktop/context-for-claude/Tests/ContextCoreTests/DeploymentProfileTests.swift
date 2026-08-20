import XCTest

@testable import ContextCore

final class DeploymentProfileTests: XCTestCase {
  func testSelfHostedProfileRoutesBackendAuthAndMCPWithoutOmiFallback() throws {
    let profile = try ContextDeploymentProfile.resolve(
      bundleValues: [:],
      environment: [
        "OMI_DEPLOYMENT_PROFILE": "self_hosted",
        "OMI_AUTH_PROVIDER": "better_auth",
        "OMI_PYTHON_API_URL": "http://127.0.0.1:8000",
        "OMI_AUTH_SERVER_URL": "http://127.0.0.1:3000",
        "OMI_MCP_API_URL": "http://127.0.0.1:9000",
      ],
      allowsEnvironmentOverrides: true)

    XCTAssertEqual(profile.backendBaseURL.absoluteString, "http://127.0.0.1:8000/")
    XCTAssertEqual(profile.authBaseURL.absoluteString, "http://127.0.0.1:3000/")
    XCTAssertEqual(profile.mcpBaseURL.absoluteString, "http://127.0.0.1:9000/")
    XCTAssertEqual(profile.identityProvider, .betterAuth)
  }

  func testReleaseProfileRejectsInsecureSelfHostedAuth() {
    XCTAssertThrowsError(
      try ContextDeploymentProfile.resolve(
        bundleValues: [
          "OmiDeploymentProfile": "self_hosted",
          "OmiAuthProvider": "better_auth",
          "OmiBackendBaseURL": "https://backend.example.com",
          "OmiAuthBaseURL": "http://auth.example.com",
        ],
        allowsEnvironmentOverrides: false,
        requiresHTTPS: true)
    ) { error in
      XCTAssertEqual(error as? ContextDeploymentProfileError, .insecureReleaseURL("OMI_AUTH_SERVER_URL"))
    }
  }

  func testSelfHostedProfileRejectsFirebaseIdentity() {
    XCTAssertThrowsError(
      try ContextDeploymentProfile.resolve(
        bundleValues: [
          "OmiDeploymentProfile": "self_hosted",
          "OmiAuthProvider": "firebase",
        ],
        allowsEnvironmentOverrides: false)
    ) { error in
      XCTAssertEqual(error as? ContextDeploymentProfileError, .invalidIdentityProvider)
    }
  }

  func testSelfHostedProfileRejectsLegacyFirebaseSession() throws {
    let profile = try ContextDeploymentProfile.resolve(
      bundleValues: [
        "OmiDeploymentProfile": "self_hosted",
        "OmiAuthProvider": "better_auth",
        "OmiBackendBaseURL": "https://backend.example.test/",
        "OmiAuthBaseURL": "https://auth.example.test/",
        "OmiMCPBaseURL": "https://mcp.example.test/",
      ],
      allowsEnvironmentOverrides: false,
      requiresHTTPS: true)

    XCTAssertFalse(profile.acceptsStoredIdentityProvider(nil))
    XCTAssertFalse(profile.acceptsStoredIdentityProvider(.firebase))
    XCTAssertTrue(profile.acceptsStoredIdentityProvider(.betterAuth))
  }
}
