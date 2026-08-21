// Platform-aware notification service with FCM implementation
// FCM Implementation: Full Firebase Cloud Messaging support (iOS, Android)

import 'package:flutter/foundation.dart';

import 'package:omi/env/env.dart';
import 'package:omi/env/environment_profile.dart';
import 'package:omi/env/firebase_services_policy.dart';
import 'package:omi/services/notifications/notification_interface.dart';
import 'package:omi/services/notifications/notification_service_basic.dart' as basic;
import 'package:omi/services/notifications/notification_service_fcm.dart' as fcm;

export 'package:omi/services/notifications/notification_interface.dart';

typedef NotificationProviderFactory<T> = T Function();

/// Singleton notification service instance
/// Automatically selects the correct platform-specific implementation
class NotificationService {
  static NotificationInterface? _instance;

  /// Get the singleton notification service instance
  static NotificationInterface get instance {
    _instance ??= createForDeployment<NotificationInterface>(
      profile: Env.profile,
      firebaseServicesEnabled: FirebaseServicesPolicy.configuredEnabled,
      createFirebaseProvider: fcm.createNotificationService,
      createNonFirebaseProvider: basic.createNotificationService,
    );
    return _instance!;
  }

  /// Selects a provider before either factory is evaluated. In particular,
  /// self-hosted releases cannot construct the FCM singleton even if a stale
  /// build flag says Firebase is enabled.
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

  /// Clear the instance (useful for testing)
  static void reset() {
    _instance = null;
  }
}
