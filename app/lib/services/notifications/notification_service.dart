// Platform-aware notification service with FCM implementation
// FCM Implementation: Full Firebase Cloud Messaging support (iOS, Android)

import 'package:flutter/foundation.dart';

import 'package:omi/env/env.dart';
import 'package:omi/env/environment_profile.dart';
import 'package:omi/env/firebase_services_policy.dart';
import 'package:omi/services/notifications/notification_interface.dart';
import 'package:omi/services/notifications/notification_service_basic.dart' as basic;
import 'package:omi/services/notifications/notification_service_fcm.dart' as fcm;
import 'package:omi/services/notifications/notification_service_operator.dart' as operator_notifications;
import 'package:omi/services/auth_service.dart';

export 'package:omi/services/notifications/notification_interface.dart';

typedef NotificationProviderFactory<T> = T Function();

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
    _instance ??= _createConfiguredService();
    return _instance!;
  }

  static NotificationInterface _createConfiguredService() {
    final operatorPushBaseUrl = Env.operatorPushRegistrationBaseUrl;
    if (Env.profile == AppEnvironmentProfile.selfHosted && operatorPushBaseUrl != null) {
      return operator_notifications.createNotificationService(registrationBaseUrl: operatorPushBaseUrl);
    }
    return createForDeployment<NotificationInterface>(
      profile: Env.profile,
      firebaseServicesEnabled: AuthService.firebaseServicesEnabled,
      createFirebaseProvider: fcm.createNotificationService,
      createNonFirebaseProvider: basic.createNotificationService,
    );
  }

  /// Select a notification implementation before evaluating either factory.
  /// This keeps self-hosted builds from constructing Firebase Messaging even
  /// when a stale build flag says Firebase is enabled.
  @visibleForTesting
  static T createForDeployment<T>({
    required AppEnvironmentProfile profile,
    required bool firebaseServicesEnabled,
    required NotificationProviderFactory<T> createFirebaseProvider,
    required NotificationProviderFactory<T> createNonFirebaseProvider,
  }) {
    if (FirebaseServicesPolicy.allowsFor(profile: profile, configuredEnabled: firebaseServicesEnabled)) {
      return createFirebaseProvider();
    }
    return createNonFirebaseProvider();
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
