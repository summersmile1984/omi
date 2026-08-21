// Platform-aware notification service with FCM implementation
// FCM Implementation: Full Firebase Cloud Messaging support (iOS, Android)

import 'package:flutter/foundation.dart';

import 'package:omi/services/notifications/notification_interface.dart';
import 'package:omi/services/notifications/notification_service_basic.dart' as basic;
import 'package:omi/services/notifications/notification_service_fcm.dart' as fcm;
import 'package:omi/services/auth_service.dart';

/// Select the notification plane before constructing any plugin-backed object.
///
/// FirebaseMessaging.instance throws `core/no-app` when Firebase has not been
/// configured. Self-hosted Better Auth builds deliberately skip Firebase, so
/// selection must happen before the FCM service's field initializer runs.
NotificationInterface _createPlatformNotificationService({required bool firebaseServicesEnabled}) {
  return firebaseServicesEnabled ? fcm.createNotificationService() : basic.createNotificationService();
}

/// Singleton notification service instance
/// Automatically selects the correct platform-specific implementation
class NotificationService {
  static NotificationInterface? _instance;

  /// Get the singleton notification service instance
  static NotificationInterface get instance {
    _instance ??= _createPlatformNotificationService(firebaseServicesEnabled: AuthService.firebaseServicesEnabled);
    return _instance!;
  }

  /// Construct a service for policy tests without mutating the application
  /// singleton or touching Firebase plugin state.
  @visibleForTesting
  static NotificationInterface createForTesting({required bool firebaseServicesEnabled}) {
    return _createPlatformNotificationService(firebaseServicesEnabled: firebaseServicesEnabled);
  }

  /// Clear the instance (useful for testing)
  static void reset() {
    _instance = null;
  }
}
