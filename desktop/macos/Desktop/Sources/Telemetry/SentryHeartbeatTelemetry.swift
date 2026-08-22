import Sentry

/// Low-noise session heartbeat for Sentry breadcrumbs — must not call
/// `SentrySDK.capture(message:)` (see issue #9191).
enum SentryHeartbeatTelemetry {
  static func shouldRecordSessionHeartbeat(allowsOmiManagedServices: Bool) -> Bool {
    allowsOmiManagedServices
  }

  static func makeSessionHeartbeatBreadcrumb() -> Breadcrumb {
    let breadcrumb = Breadcrumb(level: .info, category: "session")
    breadcrumb.message = "Session Heartbeat"
    breadcrumb.data = ["event_type": "heartbeat"]
    return breadcrumb
  }

  static func recordSessionHeartbeat() {
    guard
      shouldRecordSessionHeartbeat(
        allowsOmiManagedServices: DesktopBackendEnvironment.allowsOmiManagedServices
      )
    else { return }
    SentrySDK.addBreadcrumb(makeSessionHeartbeatBreadcrumb())
  }
}
