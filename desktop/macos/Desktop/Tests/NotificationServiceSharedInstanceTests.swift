import XCTest

@testable import Omi_Computer

/// The `NotificationService.shared` singleton is touched — directly, or indirectly
/// via `AppState.handleListenEvent`'s "proactive_message" case — by any test that
/// exercises a proactive-notification code path without constructing its own
/// `NotificationService` instance. Its initializer must never crash the process
/// when that first touch happens inside XCTest's command-line test host, which has
/// no real app bundle identity for `UNUserNotificationCenter` to attach to.
@MainActor
final class NotificationServiceSharedInstanceTests: XCTestCase {
  func testSharedInstanceDoesNotCrashUnderXCTest() {
    // Pre-fix, merely evaluating `.shared` crashed the whole xctest process inside
    // `UNUserNotificationCenter.current()` — there is nothing to assert beyond
    // reaching the next line.
    _ = NotificationService.shared
  }
}
