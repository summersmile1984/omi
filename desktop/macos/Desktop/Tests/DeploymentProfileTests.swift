import XCTest

@testable import Omi_Computer

private actor ModelEgressInvocationProbe {
  private(set) var count = 0

  func invoke() {
    count += 1
  }
}

final class DeploymentProfileTests: XCTestCase {
  func testSelfHostedProfileSelectsBetterAuthByDefault() {
    XCTAssertEqual(DesktopDeploymentProfile.resolve("self_hosted"), .selfHosted)
    XCTAssertEqual(
      DesktopBackendEnvironment.identityProvider(deploymentProfile: .selfHosted, configuredValue: nil),
      .betterAuth
    )
    XCTAssertFalse(DesktopBackendEnvironment.allowsOmiManagedServices(deploymentProfile: .selfHosted))
    XCTAssertFalse(DesktopBackendEnvironment.shouldConfigureFirebaseSDK(identityProvider: .betterAuth))
    XCTAssertTrue(DesktopBackendEnvironment.shouldConfigureFirebaseSDK(identityProvider: .firebase))
  }

  func testSelfHostedProfileAcceptsOnlyTheBetterAuthRuntimePath() {
    XCTAssertEqual(
      DesktopBackendEnvironment.identityProvider(
        deploymentProfile: .selfHosted,
        configuredValue: "better_auth"),
      .betterAuth
    )
    XCTAssertFalse(DesktopBackendEnvironment.shouldConfigureFirebaseSDK(identityProvider: .betterAuth))
  }

  func testSelfHostedModelEgressUsesBackendCapabilitiesAndRejectsDirectAdapters() {
    XCTAssertEqual(
      DesktopModelEgressPolicy.proactiveRoute(deploymentProfile: .selfHosted),
      .providerNeutralBackendCapability)
    XCTAssertFalse(
      DesktopModelEgressPolicy.allowsClientDirectVendorEgress(
        deploymentProfile: .selfHosted))
    XCTAssertFalse(DesktopModelEgressPolicy.allowsBYOK(deploymentProfile: .selfHosted))
    XCTAssertTrue(
      DesktopModelEgressPolicy.allowsAgentAdapter(
        AgentAdapterId.piMono.rawValue,
        deploymentProfile: .selfHosted))
    XCTAssertFalse(
      DesktopModelEgressPolicy.allowsAgentAdapter(
        AgentAdapterId.acp.rawValue,
        deploymentProfile: .selfHosted))

    XCTAssertEqual(
      DesktopModelEgressPolicy.realtimeRelaySelection(
        deploymentProfile: .selfHosted,
        configuredProvider: nil),
      .unavailable)
    XCTAssertEqual(
      DesktopModelEgressPolicy.realtimeRelaySelection(
        deploymentProfile: .selfHosted,
        configuredProvider: "openai"),
      .backend(provider: "openai"))
  }

  func testCloudModelEgressBehaviorRemainsUnchanged() {
    XCTAssertEqual(
      DesktopModelEgressPolicy.proactiveRoute(deploymentProfile: .omiCloud),
      .vendorSpecificBackendProxy)
    XCTAssertTrue(
      DesktopModelEgressPolicy.allowsClientDirectVendorEgress(
        deploymentProfile: .omiCloud))
    XCTAssertTrue(DesktopModelEgressPolicy.allowsBYOK(deploymentProfile: .omiCloud))
    XCTAssertEqual(
      DesktopModelEgressPolicy.realtimeRelaySelection(
        deploymentProfile: .omiCloud,
        configuredProvider: nil),
      .cloudPreference)
  }

  func testSelfHostedTaskAgentRejectsLocalClaudeCLIWhileCloudKeepsIt() {
    XCTAssertFalse(TaskAgentManager.allowsClaudeTaskAgent(deploymentProfile: .selfHosted))
    XCTAssertTrue(TaskAgentManager.allowsClaudeTaskAgent(deploymentProfile: .omiCloud))
  }

  func testSelfHostedBYOKValidationFailsBeforeTransport() async {
    let probe = ModelEgressInvocationProbe()
    let status = await BYOKValidator.validate(
      .openai,
      key: "operator-key",
      deploymentProfile: .selfHosted,
      transport: { _, _ in
        await probe.invoke()
        return .ok
      })

    guard case .failed(let message) = status else {
      return XCTFail("self-hosted BYOK validation must fail closed")
    }
    XCTAssertTrue(message.contains("backend"))
    let invocationCount = await probe.count
    XCTAssertEqual(invocationCount, 0)
  }

  func testProviderNeutralProactiveSchemaRemovesGeminiTypeDialect() throws {
    let schema = GeminiClient.providerNeutralJSONSchema([
      "type": "OBJECT",
      "properties": [
        "items": [
          "type": "ARRAY",
          "items": [
            "type": "OBJECT",
            "properties": ["title": ["type": "STRING"]],
            "required": ["title"],
          ],
        ]
      ],
      "required": ["items"],
    ])
    XCTAssertEqual(schema["type"] as? String, "object")
    XCTAssertEqual(schema["additionalProperties"] as? Bool, false)
    let properties = try XCTUnwrap(schema["properties"] as? [String: Any])
    let items = try XCTUnwrap(properties["items"] as? [String: Any])
    XCTAssertEqual(items["type"] as? String, "array")
    let itemSchema = try XCTUnwrap(items["items"] as? [String: Any])
    XCTAssertEqual(itemSchema["type"] as? String, "object")
    XCTAssertEqual(itemSchema["additionalProperties"] as? Bool, false)
  }

  func testSelfHostedReleaseUsesConfiguredServingAndAuthOrigins() {
    XCTAssertEqual(
      DesktopBackendEnvironment.pythonBaseURL(
        useDevelopmentBackends: false,
        bundleIdentifier: AppBuild.productionBundleIdentifier,
        environmentValue: "https://api.fork.example",
        deploymentProfile: .selfHosted
      ),
      "https://api.fork.example/"
    )
    XCTAssertEqual(
      DesktopBackendEnvironment.authBaseURL(
        useDevelopmentBackends: false,
        bundleIdentifier: AppBuild.productionBundleIdentifier,
        environmentValue: "https://auth.fork.example",
        deploymentProfile: .selfHosted
      ),
      "https://auth.fork.example/"
    )
    XCTAssertEqual(
      DesktopBackendEnvironment.mcpBaseURL(
        useDevelopmentBackends: false,
        bundleIdentifier: AppBuild.productionBundleIdentifier,
        environmentValue: "https://mcp.fork.example",
        deploymentProfile: .selfHosted
      ),
      "https://mcp.fork.example/"
    )
    XCTAssertEqual(
      DesktopBackendEnvironment.rustBackendURL(
        useDevelopmentBackends: false,
        bundleIdentifier: AppBuild.productionBundleIdentifier,
        environmentValue: "https://desktop.fork.example",
        launchEnvironmentValue: nil,
        deploymentProfile: .selfHosted
      ),
      "https://desktop.fork.example/"
    )
  }

  func testProductionDeploymentKeysComeFromSignedBundleNotHostEnvironment() {
    let host = [
      "OMI_DEPLOYMENT_PROFILE": "omi_cloud",
      "OMI_AUTH_SERVER_URL": "https://attacker.example",
      "OMI_SHARE_BASE_URL": "https://attacker.example",
      "OMI_REALTIME_MODEL_PROVIDER": "gemini",
      "OMI_MCP_CHATGPT_OAUTH_CLIENT_ID": "attacker-client",
      "OMI_MCP_CLAUDE_OAUTH_CLIENT_ID": "attacker-client",
    ]
    for key in host.keys {
      XCTAssertTrue(
        BundleEnvironment.shouldApplyBundledValue(
          for: key,
          launchEnvironment: host,
          bundleIdentifier: AppBuild.productionBundleIdentifier
        )
      )
      XCTAssertTrue(
        BundleEnvironment.isProductionSignedDeploymentKey(
          key,
          bundleIdentifier: AppBuild.productionBundleIdentifier
        )
      )
    }
  }

  func testSelfHostedMemoryExportDoesNotBorrowOmiOAuthRegistration() {
    XCTAssertNil(
      MemoryExportDestination.chatgptOAuthClientID(
        forOAuthBaseURL: "https://auth.fork.example/",
        deploymentProfile: .selfHosted,
        configuredValue: nil))
    XCTAssertEqual(
      MemoryExportDestination.chatgptOAuthClientID(
        forOAuthBaseURL: "https://auth.fork.example/",
        deploymentProfile: .selfHosted,
        configuredValue: "fork-chatgpt-public"),
      "fork-chatgpt-public")
    XCTAssertNil(
      MemoryExportDestination.chatGPTDirectoryInstallURL(
        allowsOmiManagedServices: false))
  }

  func testAgentBackendCredentialUsesProviderNeutralWireSchema() throws {
    let body = try AgentBackendIdentityCredential(
      accessToken: "backend-jwt",
      identityProvider: .betterAuth
    ).encoded()
    let json = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: String])

    XCTAssertEqual(json["accessToken"], "backend-jwt")
    XCTAssertEqual(json["identityProvider"], "better_auth")
    XCTAssertNil(json["firebaseToken"])
    XCTAssertThrowsError(
      try AgentBackendIdentityCredential(accessToken: "  ", identityProvider: .betterAuth))
  }

  func testSelfHostedProfileRejectsLegacyFirebaseCredential() {
    XCTAssertFalse(
      DesktopBackendEnvironment.acceptsStoredIdentityProvider(
        nil,
        configuredProvider: .betterAuth))
    XCTAssertFalse(
      DesktopBackendEnvironment.acceptsStoredIdentityProvider(
        .firebase,
        configuredProvider: .betterAuth))
    XCTAssertTrue(
      DesktopBackendEnvironment.acceptsStoredIdentityProvider(
        .betterAuth,
        configuredProvider: .betterAuth))
    XCTAssertTrue(
      DesktopBackendEnvironment.acceptsStoredIdentityProvider(
        nil,
        configuredProvider: .firebase))
  }
}
