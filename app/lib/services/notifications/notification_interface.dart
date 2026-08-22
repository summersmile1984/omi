import 'dart:async';

import 'package:awesome_notifications/awesome_notifications.dart';

import 'package:omi/backend/schema/message.dart';

enum NotificationDeliveryCapability {
  firebaseRemote,
  localOnly,
  disabled;

  bool get supportsPermissionPrompt => this != disabled;
  bool get supportsRemoteRegistration => this == firebaseRemote;
}

enum RemoteNotificationActionResult { completed, unsupported }

/// Common interface for notification services across all platforms
abstract class NotificationInterface {
  NotificationDeliveryCapability get deliveryCapability;

  /// Whether this implementation talks to Firebase Cloud Messaging.
  ///
  /// Self-hosted Better Auth builds intentionally use local notifications only;
  /// exposing the boundary makes the deployment policy observable without
  /// constructing a Firebase plugin object in tests.
  bool get usesFirebaseMessaging;

  Future<void> initialize();

  Future<void> showNotification({
    required int id,
    required String title,
    required String body,
    Map<String, String?>? payload,
    bool wakeUpScreen = false,
    NotificationSchedule? schedule,
    NotificationLayout layout = NotificationLayout.Default,
  });

  Future<bool> requestNotificationPermissions();
  Future<void> register();
  Future<String> getTimeZone();
  Future<void> saveFcmToken(String? token);
  void saveNotificationToken();
  Future<bool> hasNotificationPermissions();

  Future<void> createNotification({
    String title = '',
    String body = '',
    int notificationId = 1,
    Map<String, String?>? payload,
  });

  void clearNotification(int id);
  Future<void> listenForMessages();

  Stream<ServerMessage> get listenForServerMessages;
}

extension NotificationDeploymentActions on NotificationInterface {
  Future<bool> requestNotificationPermissionsIfSupported() {
    if (!deliveryCapability.supportsPermissionPrompt) return Future.value(false);
    return requestNotificationPermissions();
  }

  Future<RemoteNotificationActionResult> registerRemoteNotificationsIfSupported() async {
    if (!deliveryCapability.supportsRemoteRegistration) return RemoteNotificationActionResult.unsupported;
    await register();
    saveNotificationToken();
    return RemoteNotificationActionResult.completed;
  }

  RemoteNotificationActionResult saveRemoteNotificationTokenIfSupported() {
    if (!deliveryCapability.supportsRemoteRegistration) return RemoteNotificationActionResult.unsupported;
    saveNotificationToken();
    return RemoteNotificationActionResult.completed;
  }
}
