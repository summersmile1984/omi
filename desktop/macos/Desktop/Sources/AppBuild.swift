// v0.12.149 release path advancement marker
import Foundation

enum DesktopUpdateUnavailableReason: String, Equatable, Sendable {
  case missingFeedURL
  case invalidFeedURL
  case missingPublicKey
}

enum DesktopUpdateAuthority: Equatable, Sendable {
  case managed(URL)
  case operatorOwned(URL)
  case unavailable(DesktopUpdateUnavailableReason)

  var feedURL: URL? {
    switch self {
    case .managed(let url), .operatorOwned(let url): return url
    case .unavailable: return nil
    }
  }

  var isAvailable: Bool { feedURL != nil }
}

enum AppBuild {
  private static let updateChannelDefaultsKey = "update_channel"
  private static let betaOverwriteMigrationKey = "didMigrateBetaOverwrite_v1"
  private static let desktopAppcastURL = URL(
    string: "https://api.omi.me/v2/desktop/appcast.xml?platform=macos"
  )!
  private static let channelProbeMainThreadBudget: TimeInterval = 1.5
  private static let channelProbeRequestTimeout: TimeInterval = 3

  /// v0.12.149 release candidate source touch.
  static let productionBundleIdentifier = "com.omi.computer-macos"
  /// The separately-installable beta app ("Omi Beta.app"). A distinct bundle id gives it
  /// its own UserDefaults domain, TCC grants, Keychain ACL, and single-instance lock, so
  /// it runs side-by-side with stable. Must stay in sync with
  /// `DesktopStorageIdentity.betaProductionBundleIdentifier` (asserted by a unit test).
  static let betaProductionBundleIdentifier = "com.omi.computer-macos.beta"
  static let productionFamilyBundleIdentifiers: Set<String> = [
    productionBundleIdentifier, betaProductionBundleIdentifier,
  ]
  static let desktopDevBundleIdentifier = "com.omi.desktop-dev"
  static let externalPreviewBundleIdentifierPrefix = "com.omi.preview."
  static let externalPreviewMarkerInfoKey = "OMIExternalPreview"
  static let externalPreviewBackendInfoKey = "OMIExternalPreviewBackend"

  enum ExternalPreviewBackend: String, Equatable {
    case production
    case development

    init?(infoValue: Any?) {
      guard let rawValue = infoValue as? String else { return nil }
      self.init(rawValue: rawValue.trimmingCharacters(in: .whitespacesAndNewlines).lowercased())
    }
  }

  /// Preview bundle identity, the explicit Info.plist marker, and the selected backend are
  /// all evaluated together. The reserved identity is the safety boundary: an artifact with
  /// a preview identity is always restricted, even if a packaging error omits its marker.
  struct Configuration: Equatable {
    let bundleIdentifier: String
    let isExternalPreview: Bool
    let hasExternalPreviewMarker: Bool
    let externalPreviewBackend: ExternalPreviewBackend?

    var isNonProduction: Bool {
      bundleIdentifier.hasPrefix("com.omi.")
        && !AppBuild.productionFamilyBundleIdentifiers.contains(bundleIdentifier)
    }

    var allowsLocalAutomation: Bool {
      isNonProduction && !isExternalPreview
    }

    var isNamedDevelopmentBundle: Bool {
      isNonProduction && !isExternalPreview && bundleIdentifier != AppBuild.desktopDevBundleIdentifier
    }

    var allowsSparkleUpdates: Bool {
      !isExternalPreview && !isNamedDevelopmentBundle
    }

    var hasValidExternalPreviewConfiguration: Bool {
      !isExternalPreview || (hasExternalPreviewMarker && externalPreviewBackend != nil)
    }
  }

  static func configuration(
    bundleIdentifier: String,
    infoDictionary: [String: Any]
  ) -> Configuration {
    let isExternalPreview = isExternalPreviewBundleIdentifier(bundleIdentifier)
    let hasExternalPreviewMarker = infoDictionary[externalPreviewMarkerInfoKey] as? Bool == true
    let externalPreviewBackend = ExternalPreviewBackend(
      infoValue: infoDictionary[externalPreviewBackendInfoKey])

    return Configuration(
      bundleIdentifier: bundleIdentifier,
      isExternalPreview: isExternalPreview,
      hasExternalPreviewMarker: hasExternalPreviewMarker,
      externalPreviewBackend: externalPreviewBackend
    )
  }

  static func isExternalPreviewBundleIdentifier(_ bundleIdentifier: String) -> Bool {
    let suffix = bundleIdentifier.dropFirst(externalPreviewBundleIdentifierPrefix.count)
    return bundleIdentifier.hasPrefix(externalPreviewBundleIdentifierPrefix) && !suffix.isEmpty
  }

  private static var buildConfiguration: Configuration {
    configuration(
      bundleIdentifier: bundleIdentifier,
      infoDictionary: Bundle.main.infoDictionary ?? [:]
    )
  }

  static var bundleIdentifier: String {
    Bundle.main.bundleIdentifier ?? productionBundleIdentifier
  }

  static var isNonProduction: Bool {
    buildConfiguration.isNonProduction
  }

  /// True for every shipped production-family artifact (stable *and* the beta app).
  /// Use `isBetaProductionBundle` when behavior differs between the two.
  static var isProductionBundle: Bool {
    productionFamilyBundleIdentifiers.contains(bundleIdentifier)
  }

  static func firebaseAPIKey(bundleIdentifier: String, environmentKey: String?, bundledKey: String?) -> String {
    // Shipped Beta shares production Firebase identity even while serving through dev.
    if productionFamilyBundleIdentifiers.contains(bundleIdentifier) {
      return bundledKey?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }
    return environmentKey?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
  }

  /// The separately-installable "Omi Beta" app. Its update channel is pinned to beta
  /// and it keeps its own isolated on-disk state, so it can run beside stable.
  static var isBetaProductionBundle: Bool {
    bundleIdentifier == betaProductionBundleIdentifier
  }

  static var isExternalPreview: Bool {
    buildConfiguration.isExternalPreview
  }

  /// Legacy "Omi Computer.app" cleanup force-terminates running
  /// `com.omi.computer-macos` processes and deletes the old bundle — strictly
  /// stable-lineage housekeeping. Only the stable identity may run it: Omi Beta
  /// or a dev bundle doing so would kill the user's running stable app.
  static var mayRunLegacyStableAppCleanup: Bool {
    mayRunLegacyStableAppCleanup(bundleIdentifier: bundleIdentifier)
  }

  static func mayRunLegacyStableAppCleanup(bundleIdentifier: String) -> Bool {
    bundleIdentifier == productionBundleIdentifier
  }

  /// Only local development bundles expose the loopback automation/debug bridge. Published
  /// preview apps share the non-production namespace but must never expose that bridge.
  static var allowsLocalAutomation: Bool {
    buildConfiguration.allowsLocalAutomation
  }

  /// Preview artifacts and local named developer bundles never consume the shared Sparkle feed.
  /// The updater additionally checks this at every call site.
  static var allowsSparkleUpdates: Bool {
    allowsSparkleUpdates(
      deploymentProfile: DesktopBackendEnvironment.deploymentProfile,
      authority: updateAuthority)
  }

  static func allowsSparkleUpdates(deploymentProfile: DesktopDeploymentProfile) -> Bool {
    buildConfiguration.allowsSparkleUpdates
      && DesktopBackendEnvironment.allowsOmiManagedServices(deploymentProfile: deploymentProfile)
  }

  static func allowsSparkleUpdates(
    deploymentProfile: DesktopDeploymentProfile,
    authority: DesktopUpdateAuthority
  ) -> Bool {
    guard buildConfiguration.allowsSparkleUpdates else { return false }
    switch (deploymentProfile, authority) {
    case (.omiCloud, .managed): return true
    case (.selfHosted, .operatorOwned): return true
    default: return false
    }
  }

  static var updateAuthority: DesktopUpdateAuthority {
    resolveUpdateAuthority(
      deploymentProfile: DesktopBackendEnvironment.deploymentProfile,
      feedURLValue: DesktopBackendEnvironment.currentEnvironmentValue("OMI_UPDATE_FEED_URL"),
      publicKeyValue: Bundle.main.object(forInfoDictionaryKey: "SUPublicEDKey") as? String,
      bundleIdentifier: bundleIdentifier)
  }

  static var updateFeedURL: URL? { updateAuthority.feedURL }

  /// Resolve the update source before Sparkle is constructed. Operator feeds
  /// are only enabled when the feed URL is an explicit HTTPS/path endpoint and
  /// the signed bundle carries a valid 32-byte Sparkle Ed25519 public key.
  static func resolveUpdateAuthority(
    deploymentProfile: DesktopDeploymentProfile,
    feedURLValue: String?,
    publicKeyValue: String?,
    bundleIdentifier: String
  ) -> DesktopUpdateAuthority {
    guard configuration(bundleIdentifier: bundleIdentifier, infoDictionary: [:]).allowsSparkleUpdates else {
      return .unavailable(.invalidFeedURL)
    }
    if deploymentProfile == .omiCloud {
      return .managed(desktopAppcastURL)
    }
    guard let feedURLValue, !feedURLValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
      return .unavailable(.missingFeedURL)
    }
    guard
      let feedURL = canonicalOperatorUpdateFeedURL(
        feedURLValue,
        bundleIdentifier: bundleIdentifier
      )
    else {
      return .unavailable(.invalidFeedURL)
    }
    guard let publicKeyValue,
      let keyData = Data(base64Encoded: publicKeyValue.trimmingCharacters(in: .whitespacesAndNewlines)),
      keyData.count == 32
    else {
      return .unavailable(.missingPublicKey)
    }
    return .operatorOwned(feedURL)
  }

  static func canonicalOperatorUpdateFeedURL(
    _ raw: String,
    bundleIdentifier: String
  ) -> URL? {
    guard var components = URLComponents(string: raw),
      let scheme = components.scheme?.lowercased(),
      let host = components.host,
      !host.isEmpty,
      components.user == nil,
      components.password == nil,
      components.query == nil,
      components.fragment == nil,
      !components.path.isEmpty,
      components.path != "/",
      scheme == "http" || scheme == "https"
    else { return nil }
    if scheme == "http" && productionFamilyBundleIdentifiers.contains(bundleIdentifier) {
      return nil
    }
    let origin = components.port.map { "\(scheme)://\(host):\($0)" } ?? "\(scheme)://\(host)"
    guard
      let canonicalOrigin = try? DesktopBackendEnvironment.canonicalSelfHostedOrigin(
        origin,
        key: "OMI_UPDATE_FEED_URL",
        requiresHTTPS: productionFamilyBundleIdentifiers.contains(bundleIdentifier)
      ),
      let originComponents = URLComponents(string: canonicalOrigin)
    else { return nil }
    components.scheme = originComponents.scheme
    components.host = originComponents.host
    components.path = components.path.hasPrefix("/") ? components.path : "/\(components.path)"
    return components.url
  }

  static var hasValidExternalPreviewConfiguration: Bool {
    buildConfiguration.hasValidExternalPreviewConfiguration
  }

  /// Nil is intentional for a malformed preview configuration. Backend routing then fails
  /// closed to production rather than inheriting the local-development default.
  static var externalPreviewBackend: ExternalPreviewBackend? {
    guard buildConfiguration.isExternalPreview, buildConfiguration.hasExternalPreviewMarker else {
      return nil
    }
    return buildConfiguration.externalPreviewBackend
  }

  static var isNamedDevelopmentBundle: Bool {
    buildConfiguration.isNamedDevelopmentBundle
  }

  static var usesLazyDevPermissions: Bool {
    isNamedDevelopmentBundle && UserDefaults.standard.bool(forKey: "devLazyPermissionsEnabled")
  }

  static var displayName: String {
    if let displayName = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String,
      !displayName.isEmpty
    {
      return displayName
    }

    if let bundleName = Bundle.main.object(forInfoDictionaryKey: "CFBundleName") as? String,
      !bundleName.isEmpty
    {
      return bundleName
    }

    return "omi"
  }

  /// GitHub repo that hosts desktop releases (source of truth for the changelog).
  private static let releasesBaseURL = "https://github.com/BasedHardware/omi/releases"

  /// Release tag for the running build, e.g. "v0.11.475+11475-macos".
  /// Matches the tag Codemagic publishes (`v{shortVersion}+{build}-{platform}`).
  static var releaseTag: String? {
    guard
      let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String,
      !version.isEmpty,
      let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String,
      !build.isEmpty
    else {
      return nil
    }
    return "v\(version)+\(build)-macos"
  }

  /// "What's New" target: the GitHub release page for the running build.
  /// Real shipped builds (beta + stable both use the production bundle id) carry a
  /// version that maps to a published tag, so deep-link to this version's notes (the
  /// `+` in the tag must be `%2B` in the URL path). Dev/named test bundles carry a
  /// placeholder version with no matching tag, so fall back to the releases list.
  static var changelogURLString: String {
    guard isProductionBundle, let tag = releaseTag else { return releasesBaseURL }
    return "\(releasesBaseURL)/tag/\(tag.replacingOccurrences(of: "+", with: "%2B"))"
  }

  /// Sparkle channel is identity-bound. Omi Beta is permanently a beta-channel
  /// client. Stable.app never consumes the beta Sparkle channel: leftover
  /// `update_channel` defaults and server-synced settings must not opt it into
  /// newer stable-identity zips against production APIs.
  static var currentUpdateChannel: String {
    updateChannel(isBetaIdentity: isBetaProductionBundle)
  }

  static func updateChannel(isBetaIdentity: Bool) -> String {
    isBetaIdentity ? "beta" : "stable"
  }

  static var manualDownloadURL: URL {
    manualDownloadURL(
      channel: currentUpdateChannel,
      isBetaIdentity: isBetaProductionBundle,
      deploymentProfile: DesktopBackendEnvironment.deploymentProfile,
      backendBaseURL: DesktopBackendEnvironment.pythonBaseURL()
    )
  }

  /// Fail-closed Omi Beta download. Stable Settings uses this instead of flipping Sparkle.
  static var omiBetaInstallURL: URL {
    manualDownloadURL(channel: "beta", isBetaIdentity: true)
  }

  static func manualDownloadURL(
    channel: String,
    isBetaIdentity: Bool,
    deploymentProfile: DesktopDeploymentProfile = .omiCloud,
    backendBaseURL: String? = nil
  ) -> URL {
    var components = URLComponents()
    components.scheme = "https"
    if deploymentProfile == .selfHosted {
      guard let backendBaseURL,
        let canonicalBaseURL = try? DesktopBackendEnvironment.canonicalSelfHostedOrigin(
          backendBaseURL,
          key: "OMI_DESKTOP_API_URL",
          requiresHTTPS: productionFamilyBundleIdentifiers.contains(bundleIdentifier)),
        let baseURL = URL(string: canonicalBaseURL),
        let scheme = baseURL.scheme,
        let host = baseURL.host,
        !host.isEmpty,
        ["http", "https"].contains(scheme.lowercased())
      else {
        // There is no safe managed-download fallback in self-hosted mode.
        return URL(fileURLWithPath: "/")
      }
      components.scheme = scheme
      components.host = host
      components.port = baseURL.port
      components.path = "/v2/desktop/download/latest"
    } else {
      components.host = "api.omi.me"
      components.path = "/v2/desktop/download/latest"
    }
    var queryItems = [URLQueryItem(name: "channel", value: channel)]
    if isBetaIdentity {
      // The Omi Beta app must re-download its own identity, never the stable app.
      queryItems.append(URLQueryItem(name: "identity", value: "beta"))
    }
    components.queryItems = queryItems
    guard let url = components.url else {
      preconditionFailure("desktop download URL could not be constructed")
    }
    return url
  }

  static var inferredUpdateChannel: String {
    let bundlePath = Bundle.main.bundleURL.path.lowercased()
    let display = displayName.lowercased()
    let bundle = bundleIdentifier.lowercased()

    if bundle.contains("beta")
      || display.contains("beta")
      || bundlePath.contains("/beta")
      || bundlePath.contains("omi beta")
    {
      return "beta"
    }

    return "stable"
  }

  /// Only set the channel on first launch when no preference exists yet.
  /// Never overwrite a user-chosen channel (e.g. beta selected in settings).
  @discardableResult
  static func syncUpdateChannelOnFirstLaunch() -> String? {
    guard allowsSparkleUpdates else { return nil }
    guard UserDefaults.standard.string(forKey: updateChannelDefaultsKey) == nil else { return nil }
    let resolved = probeFreshInstallUpdateChannel()
    UserDefaults.standard.set(resolved, forKey: updateChannelDefaultsKey)
    return resolved
  }

  /// One-time migration for users whose beta channel was overwritten to stable
  /// by the syncUpdateChannelWithInstalledApp() bug (commit 8c60fafe8, March 27 2026).
  /// Re-checks the appcast: if the current build is ahead of latest stable, restore beta.
  static func migrateBetaChannelOverwrite() {
    guard allowsSparkleUpdates else { return }
    migrateBetaChannelOverwrite(probeAppcast: probeFreshInstallUpdateChannel)
  }

  static func migrateBetaChannelOverwrite(probeAppcast: () -> String) {
    guard !UserDefaults.standard.bool(forKey: betaOverwriteMigrationKey) else { return }
    UserDefaults.standard.set(true, forKey: betaOverwriteMigrationKey)

    // A fresh install has no stored channel, so there is nothing to restore — and
    // syncUpdateChannelOnFirstLaunch() probes the same appcast moments later. Probing
    // here as well made every new install pay for two serial launch-blocking round
    // trips to answer one question.
    guard UserDefaults.standard.string(forKey: updateChannelDefaultsKey) != nil else { return }
    guard currentUpdateChannel == "stable" else { return }

    if probeAppcast() == "beta" {
      UserDefaults.standard.set("beta", forKey: updateChannelDefaultsKey)
    }
  }

  static func prepareUpdateChannelForBackendRouting() {
    guard isProductionBundle, allowsSparkleUpdates else { return }
    // Beta identity: channel is pinned, so the launch-blocking appcast probes and the
    // stable-overwrite migration have nothing to decide.
    guard !isBetaProductionBundle else { return }

    migrateBetaChannelOverwrite()
    if UserDefaults.standard.string(forKey: updateChannelDefaultsKey) == nil {
      syncUpdateChannelOnFirstLaunch()
    }
  }

  static func resolveFreshInstallUpdateChannel(
    currentBuild: Int,
    fallback: String,
    appcastXML: String
  ) -> String {
    if fallback == "beta" {
      return "beta"
    }

    guard let latestStableBuild = latestStableBuildNumber(in: appcastXML) else {
      return fallback
    }

    return currentBuild > latestStableBuild ? "beta" : "stable"
  }

  static func latestStableBuildNumber(in appcastXML: String) -> Int? {
    let itemPattern = #"<item>(.*?)</item>"#
    let versionPattern = #"<sparkle:version>(\d+)</sparkle:version>"#

    guard
      let itemRegex = try? NSRegularExpression(
        pattern: itemPattern,
        options: [.dotMatchesLineSeparators]
      ),
      let versionRegex = try? NSRegularExpression(pattern: versionPattern)
    else {
      return nil
    }

    let xmlRange = NSRange(appcastXML.startIndex..<appcastXML.endIndex, in: appcastXML)
    var latestStableBuild: Int?

    for match in itemRegex.matches(in: appcastXML, options: [], range: xmlRange) {
      guard
        let itemRange = Range(match.range(at: 1), in: appcastXML)
      else {
        continue
      }

      let itemXML = String(appcastXML[itemRange])
      if itemXML.contains("<sparkle:channel>beta</sparkle:channel>")
        || itemXML.contains("<sparkle:channel>staging</sparkle:channel>")
      {
        continue
      }

      let itemNSRange = NSRange(itemXML.startIndex..<itemXML.endIndex, in: itemXML)
      guard
        let versionMatch = versionRegex.firstMatch(in: itemXML, options: [], range: itemNSRange),
        let versionRange = Range(versionMatch.range(at: 1), in: itemXML),
        let build = Int(itemXML[versionRange])
      else {
        continue
      }

      latestStableBuild = max(latestStableBuild ?? build, build)
    }

    return latestStableBuild
  }

  private static var currentBuildNumber: Int? {
    guard
      let raw = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String
    else {
      return nil
    }

    return Int(raw)
  }

  private static func probeFreshInstallUpdateChannel() -> String {
    probeFreshInstallUpdateChannel(
      fallback: inferredUpdateChannel,
      currentBuild: currentBuildNumber,
      mainThreadBudget: channelProbeMainThreadBudget,
      fetchAppcast: fetchDesktopAppcast,
      persistLateCorrection: { storeLateChannelCorrection($0) }
    )
  }

  /// Resolve the channel for an install with no stored preference.
  ///
  /// This runs on the main thread during launch (`AppState.init` needs the channel before
  /// it loads backend URLs), so it waits at most `mainThreadBudget` for the appcast. Past
  /// that it returns the bundle-inferred channel and lets the request finish in the
  /// background: a late answer that disagrees is written through `persistLateCorrection`,
  /// so the next launch starts on the right channel.
  ///
  /// It used to block for up to 3.5s inline, and pinned the timed-out guess permanently.
  static func probeFreshInstallUpdateChannel(
    fallback: String,
    currentBuild: Int?,
    mainThreadBudget: TimeInterval,
    fetchAppcast: @escaping (@escaping @Sendable (String?) -> Void) -> Void,
    persistLateCorrection: @escaping @Sendable (String) -> Void
  ) -> String {
    if fallback == "beta" {
      return "beta"
    }

    guard let currentBuild else {
      return fallback
    }

    let appcast = AppcastProbeResult()
    let semaphore = DispatchSemaphore(value: 0)

    fetchAppcast { xml in
      appcast.set(xml)
      semaphore.signal()
    }

    if semaphore.wait(timeout: .now() + mainThreadBudget) == .success {
      guard let appcastXML = appcast.value else { return fallback }
      return resolveFreshInstallUpdateChannel(
        currentBuild: currentBuild,
        fallback: fallback,
        appcastXML: appcastXML
      )
    }

    DispatchQueue.global(qos: .utility).async {
      guard
        semaphore.wait(timeout: .now() + channelProbeRequestTimeout + 0.5) == .success,
        let appcastXML = appcast.value
      else { return }

      let resolved = resolveFreshInstallUpdateChannel(
        currentBuild: currentBuild,
        fallback: fallback,
        appcastXML: appcastXML
      )
      guard resolved != fallback else { return }
      persistLateCorrection(resolved)
    }

    return fallback
  }

  private static func fetchDesktopAppcast(completion: @escaping @Sendable (String?) -> Void) {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.timeoutIntervalForRequest = channelProbeRequestTimeout
    configuration.timeoutIntervalForResource = channelProbeRequestTimeout

    let session = URLSession(configuration: configuration)
    /// Release path advancement v0.12.149.
    /// Fixture fix validated for release path.
    session.dataTask(with: updateFeedURL ?? desktopAppcastURL) { data, _, _ in
      defer { session.finishTasksAndInvalidate() }
      guard let data, let xml = String(data: data, encoding: .utf8) else {
        completion(nil)
        return
      }
      completion(xml)
    }.resume()
  }

  private static func storeLateChannelCorrection(_ resolved: String) {
    DispatchQueue.main.async {
      // Only upgrade the guess this probe stored — never clobber a channel the user
      // picked in Settings while the appcast was still in flight.
      guard currentUpdateChannel == "stable" else { return }
      UserDefaults.standard.set(resolved, forKey: updateChannelDefaultsKey)
      log("AppBuild: appcast answered after the launch budget; update channel set to \(resolved)")
    }
  }
}

private final class AppcastProbeResult: @unchecked Sendable {
  private let lock = NSLock()
  private var xml: String?

  func set(_ value: String?) {
    lock.lock()
    defer { lock.unlock() }
    xml = value
  }

  var value: String? {
    lock.lock()
    defer { lock.unlock() }
    return xml
  }
}
