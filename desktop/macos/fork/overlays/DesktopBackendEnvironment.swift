import Foundation

/// The selected deployment owns every endpoint. Process launch values cannot
/// switch an installed fork app to another identity or customer data plane.
enum DesktopBackendEnvironment {
  static var productionPythonAPIURL: String { normalized(ForkDesktopBuild.profile.apiBaseURL) }
  static var productionRustBackendURL: String { productionPythonAPIURL }
  static var developmentPythonAPIURL: String { productionPythonAPIURL }
  static var developmentRustBackendURL: String { productionPythonAPIURL }
  static var productionShareBaseURL: String { ForkDesktopBuild.profile.shareBaseURL.absoluteString }

  static var shouldUseDevelopmentBackends: Bool { ForkDesktopBuild.profile.stage == "local" }
  static func shouldUseDevelopmentBackends(
    bundleIdentifier: String, updateChannel: String, externalPreviewBackend: AppBuild.ExternalPreviewBackend? = nil
  ) -> Bool { shouldUseDevelopmentBackends }

  static var shouldUseProductionAuth: Bool { false }
  static func shouldUseProductionAuth(bundleIdentifier: String) -> Bool { false }
  static var shouldForceDevelopmentServingEndpoints: Bool { false }
  static func shouldForceDevelopmentServingEndpoints(bundleIdentifier: String) -> Bool { false }

  static func pythonBaseURL(environmentValue: String? = nil) -> String { productionPythonAPIURL }
  static func pythonBaseURL(
    useDevelopmentBackends: Bool, bundleIdentifier: String = AppBuild.bundleIdentifier, environmentValue: String?
  ) -> String { productionPythonAPIURL }

  static func authBaseURL(
    useDevelopmentBackends: Bool = shouldUseDevelopmentBackends,
    bundleIdentifier: String = AppBuild.bundleIdentifier, environmentValue: String? = nil
  ) -> String { normalized(ForkDesktopBuild.profile.authBaseURL) }

  static func rustBackendURL(environmentValue: String? = nil, launchEnvironmentValue: String? = nil) -> String {
    productionPythonAPIURL
  }
  static func rustBackendURL(
    useDevelopmentBackends: Bool, bundleIdentifier: String = AppBuild.bundleIdentifier,
    environmentValue: String?, launchEnvironmentValue: String?
  ) -> String { productionPythonAPIURL }

  static func shareBaseURL(environmentValue: String? = nil) -> String { productionShareBaseURL }
  static func conversationShareURL(id: String, environmentValue: String? = nil) -> String {
    ForkDesktopBuild.profile.shareBaseURL.appendingPathComponent("conversations").appendingPathComponent(id)
      .absoluteString
  }

  static func applyReleaseChannelDefaults() { ForkDesktopBuild.installEnvironment() }
  private static func normalized(_ url: URL) -> String {
    let value = url.absoluteString
    return value.hasSuffix("/") ? value : value + "/"
  }
}
