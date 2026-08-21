import Foundation

extension AgentRuntimeProcess {
  static func assertModelEgressAllowed(
    preferredAdapterId: AgentAdapterId,
    deploymentProfile: DesktopDeploymentProfile = DesktopBackendEnvironment.deploymentProfile
  ) throws {
    guard
      DesktopModelEgressPolicy.allowsAgentAdapter(
        preferredAdapterId.rawValue,
        deploymentProfile: deploymentProfile)
    else {
      log(
        "AgentRuntimeProcess: model capability unavailable adapter=\(preferredAdapterId.rawValue) profile=self_hosted"
      )
      throw BridgeError.agentError("model_capability_unavailable")
    }
  }

  static func byokEnvironmentKey(for provider: BYOKProvider) -> String {
    "OMI_BYOK_\(provider.rawValue.uppercased())"
  }

  static func removeInheritedBYOKEnvironment(from env: inout [String: String]) {
    let inheritedBYOKKeys = env.keys.filter { $0.uppercased().hasPrefix("OMI_BYOK_") }
    for key in inheritedBYOKKeys {
      env.removeValue(forKey: key)
    }
  }

  static func removeInheritedModelVendorEnvironment(
    from env: inout [String: String],
    deploymentProfile: DesktopDeploymentProfile
  ) {
    guard deploymentProfile == .selfHosted else { return }
    for key in [
      "ANTHROPIC_API_KEY",
      "CLAUDE_CODE_USE_VERTEX",
      "DEEPGRAM_API_KEY",
      "GEMINI_API_KEY",
      "GOOGLE_API_KEY",
      "OPENAI_API_KEY",
    ] {
      env.removeValue(forKey: key)
    }
  }
}
