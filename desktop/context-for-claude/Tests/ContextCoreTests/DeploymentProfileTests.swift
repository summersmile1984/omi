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
        "OMI_DESKTOP_API_URL": "http://127.0.0.1:8100",
        "OMI_AUTH_SERVER_URL": "http://127.0.0.1:3000",
        "OMI_MCP_API_URL": "http://127.0.0.1:9000",
        "OMI_SPEECH_MODEL_MODE": "disabled",
      ],
      allowsEnvironmentOverrides: true)

    XCTAssertEqual(profile.backendBaseURL.absoluteString, "http://127.0.0.1:8000/")
    XCTAssertEqual(profile.desktopBaseURL.absoluteString, "http://127.0.0.1:8100/")
    XCTAssertEqual(profile.authBaseURL.absoluteString, "http://127.0.0.1:3000/")
    XCTAssertEqual(profile.mcpBaseURL.absoluteString, "http://127.0.0.1:9000/")
    XCTAssertEqual(profile.identityProvider, .betterAuth)
    XCTAssertEqual(profile.speechModelAuthority, .disabled)
    XCTAssertNil(profile.updateFeedURL)
    XCTAssertNil(profile.updatePublicKey)
  }

  func testSelfHostedProfileCarriesOptionalOperatorUpdateAuthority() throws {
    let key = Data(repeating: 0x2A, count: 32).base64EncodedString()
    let profile = try ContextDeploymentProfile.resolve(
      bundleValues: [
        "OmiDeploymentProfile": "self_hosted",
        "OmiAuthProvider": "better_auth",
        "OmiBackendBaseURL": "https://backend.example.test/",
        "OmiDesktopBaseURL": "https://desktop.example.test/",
        "OmiAuthBaseURL": "https://auth.example.test/",
        "OmiMCPBaseURL": "https://mcp.example.test/",
        "OmiSpeechModelMode": "disabled",
        "OmiUpdateFeedURL": "HTTPS://updates.example.test:443/context/appcast.xml",
        "OmiUpdatePublicKey": "  \(key)  ",
      ],
      allowsEnvironmentOverrides: false,
      requiresHTTPS: true)

    XCTAssertEqual(profile.updateFeedURL?.absoluteString, "https://updates.example.test/context/appcast.xml")
    XCTAssertEqual(profile.updatePublicKey, key)
  }

  func testSelfHostedProfileRejectsManagedOperatorUpdateFeed() {
    XCTAssertThrowsError(
      try ContextDeploymentProfile.resolve(
        bundleValues: [
          "OmiDeploymentProfile": "self_hosted",
          "OmiAuthProvider": "better_auth",
          "OmiBackendBaseURL": "https://backend.example.test/",
          "OmiDesktopBaseURL": "https://desktop.example.test/",
          "OmiAuthBaseURL": "https://auth.example.test/",
          "OmiMCPBaseURL": "https://mcp.example.test/",
          "OmiSpeechModelMode": "disabled",
          "OmiUpdateFeedURL": "https://api.omi.me/updates/appcast.xml",
        ],
        allowsEnvironmentOverrides: false,
        requiresHTTPS: true)
    ) { error in
      XCTAssertEqual(error as? ContextDeploymentProfileError, .managedOrigin("OMI_UPDATE_FEED_URL"))
    }
  }

  func testReleaseProfileRejectsInsecureSelfHostedAuth() {
    XCTAssertThrowsError(
      try ContextDeploymentProfile.resolve(
        bundleValues: [
          "OmiDeploymentProfile": "self_hosted",
          "OmiAuthProvider": "better_auth",
          "OmiBackendBaseURL": "https://backend.example.com",
          "OmiDesktopBaseURL": "https://desktop.example.com",
          "OmiAuthBaseURL": "http://auth.example.com",
          "OmiSpeechModelMode": "disabled",
        ],
        allowsEnvironmentOverrides: false,
        requiresHTTPS: true)
    ) { error in
      XCTAssertEqual(
        error as? ContextDeploymentProfileError, .insecureReleaseURL("OMI_AUTH_SERVER_URL"))
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
        "OmiDesktopBaseURL": "https://desktop.example.test/",
        "OmiAuthBaseURL": "https://auth.example.test/",
        "OmiMCPBaseURL": "https://mcp.example.test/",
        "OmiSpeechModelMode": "disabled",
      ],
      allowsEnvironmentOverrides: false,
      requiresHTTPS: true)

    XCTAssertFalse(profile.acceptsStoredIdentityProvider(nil))
    XCTAssertFalse(profile.acceptsStoredIdentityProvider(.firebase))
    XCTAssertTrue(profile.acceptsStoredIdentityProvider(.betterAuth))
  }

  func testSelfHostedProfileRejectsManagedScreenActivityOrigin() {
    XCTAssertThrowsError(
      try ContextDeploymentProfile.resolve(
        bundleValues: [
          "OmiDeploymentProfile": "self_hosted",
          "OmiAuthProvider": "better_auth",
          "OmiBackendBaseURL": "https://backend.example.test/",
          "OmiDesktopBaseURL": "https://screen.omi.me/",
          "OmiAuthBaseURL": "https://auth.example.test/",
          "OmiMCPBaseURL": "https://mcp.example.test/",
          "OmiSpeechModelMode": "disabled",
        ],
        allowsEnvironmentOverrides: false,
        requiresHTTPS: true)
    ) { error in
      XCTAssertEqual(error as? ContextDeploymentProfileError, .managedOrigin("OMI_DESKTOP_API_URL"))
    }
  }

  func testSelfHostedProfileRejectsManagedOriginWithTrailingDotAndMixedCase() {
    XCTAssertThrowsError(
      try ContextDeploymentProfile.resolve(
        bundleValues: [
          "OmiDeploymentProfile": "self_hosted",
          "OmiAuthProvider": "better_auth",
          "OmiBackendBaseURL": "https://API.OMI.ME.:443/",
          "OmiDesktopBaseURL": "https://desktop.example.test/",
          "OmiAuthBaseURL": "https://auth.example.test/",
          "OmiSpeechModelMode": "disabled",
        ],
        allowsEnvironmentOverrides: false,
        requiresHTTPS: true)
    ) { error in
      XCTAssertEqual(error as? ContextDeploymentProfileError, .managedOrigin("OMI_PYTHON_API_URL"))
    }
  }

  func testSelfHostedProfileRejectsKnownManagedDesktopCloudRunHosts() {
    for host in [
      "DESKTOP-BACKEND-HHIBJAJAJA-UC.A.RUN.APP.",
      "DESKTOP-BACKEND-DT5LRFKKOA-UC.A.RUN.APP.",
    ] {
      XCTAssertThrowsError(
        try ContextDeploymentProfile.resolve(
          bundleValues: [
            "OmiDeploymentProfile": "self_hosted",
            "OmiAuthProvider": "better_auth",
            "OmiBackendBaseURL": "https://backend.example.test/",
            "OmiDesktopBaseURL": "https://\(host):443/",
            "OmiAuthBaseURL": "https://auth.example.test/",
            "OmiSpeechModelMode": "disabled",
          ],
          allowsEnvironmentOverrides: false,
          requiresHTTPS: true)
      ) { error in
        XCTAssertEqual(
          error as? ContextDeploymentProfileError, .managedOrigin("OMI_DESKTOP_API_URL"))
      }
    }
  }

  func testSelfHostedProfileAllowsOperatorOwnedCloudRunHost() throws {
    let profile = try ContextDeploymentProfile.resolve(
      bundleValues: [
        "OmiDeploymentProfile": "self_hosted",
        "OmiAuthProvider": "better_auth",
        "OmiBackendBaseURL": "https://backend.example.test/",
        "OmiDesktopBaseURL": "https://operator-screen-123.a.run.app/",
        "OmiAuthBaseURL": "https://auth.example.test/",
        "OmiSpeechModelMode": "disabled",
      ],
      allowsEnvironmentOverrides: false,
      requiresHTTPS: true)

    XCTAssertEqual(profile.desktopBaseURL.absoluteString, "https://operator-screen-123.a.run.app/")
  }

  func testSelfHostedProfileCanonicalizesCaseAndDefaultPorts() throws {
    let profile = try ContextDeploymentProfile.resolve(
      bundleValues: [
        "OmiDeploymentProfile": "self_hosted",
        "OmiAuthProvider": "better_auth",
        "OmiBackendBaseURL": "HTTPS://BACKEND.EXAMPLE.TEST:443/",
        "OmiDesktopBaseURL": "HTTPS://DESKTOP.EXAMPLE.TEST:443/",
        "OmiAuthBaseURL": "HTTPS://AUTH.EXAMPLE.TEST:443/",
        "OmiMCPBaseURL": "HTTPS://MCP.EXAMPLE.TEST:443/",
        "OmiSpeechModelMode": "disabled",
      ],
      allowsEnvironmentOverrides: false,
      requiresHTTPS: true)

    XCTAssertEqual(profile.backendBaseURL.absoluteString, "https://backend.example.test/")
    XCTAssertEqual(profile.desktopBaseURL.absoluteString, "https://desktop.example.test/")
    XCTAssertEqual(profile.authBaseURL.absoluteString, "https://auth.example.test/")
    XCTAssertEqual(profile.mcpBaseURL.absoluteString, "https://mcp.example.test/")
  }

  func testSelfHostedProfileRejectsScreenActivityEndpointInsteadOfSignedOrigin() {
    XCTAssertThrowsError(
      try ContextDeploymentProfile.resolve(
        bundleValues: [
          "OmiDeploymentProfile": "self_hosted",
          "OmiAuthProvider": "better_auth",
          "OmiBackendBaseURL": "https://backend.example.test/",
          "OmiDesktopBaseURL": "https://user:secret@screen.example.test/hidden?token=value",
          "OmiAuthBaseURL": "https://auth.example.test/",
          "OmiMCPBaseURL": "https://mcp.example.test/",
          "OmiSpeechModelMode": "disabled",
        ],
        allowsEnvironmentOverrides: false,
        requiresHTTPS: true)
    ) { error in
      XCTAssertEqual(error as? ContextDeploymentProfileError, .invalidURL("OMI_DESKTOP_API_URL"))
    }
  }

  func testSelfHostedSpeechModelMustBeLocalOrExplicitlyDisabled() throws {
    let base = [
      "OmiDeploymentProfile": "self_hosted",
      "OmiAuthProvider": "better_auth",
      "OmiBackendBaseURL": "https://backend.example.test/",
      "OmiDesktopBaseURL": "https://desktop.example.test/",
      "OmiAuthBaseURL": "https://auth.example.test/",
    ]
    XCTAssertThrowsError(
      try ContextDeploymentProfile.resolve(
        bundleValues: base,
        allowsEnvironmentOverrides: false,
        requiresHTTPS: true)
    ) { error in
      XCTAssertEqual(error as? ContextDeploymentProfileError, .invalidSpeechModelAuthority)
    }

    var local = base
    local["OmiSpeechModelMode"] = "packaged"
    local["OmiSpeechModelPath"] = "SpeechModel"
    let profile = try ContextDeploymentProfile.resolve(
      bundleValues: local,
      allowsEnvironmentOverrides: false,
      requiresHTTPS: true)
    XCTAssertEqual(profile.speechModelAuthority, .local(path: "SpeechModel"))

    local["OmiSpeechModelPath"] = "/tmp/untrusted"
    XCTAssertThrowsError(
      try ContextDeploymentProfile.resolve(
        bundleValues: local,
        allowsEnvironmentOverrides: false,
        requiresHTTPS: true)
    )
  }
}
