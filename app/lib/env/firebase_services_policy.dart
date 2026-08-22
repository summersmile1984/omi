/// Build-time policy for the optional Firebase SDK/data plane.
///
/// Kept independent from [AuthService] so logging, notifications, and crash
/// reporting can make the same decision without importing the auth service
/// (which itself depends on those platform helpers).
import 'package:omi/env/environment_profile.dart';

final class FirebaseServicesPolicy {
  static const _identityProvider = String.fromEnvironment('OMI_AUTH_PROVIDER', defaultValue: 'firebase');
  static const _servicesDefine = String.fromEnvironment('OMI_FIREBASE_SERVICES_ENABLED');

  static bool get enabled => enabledForTesting(provider: _identityProvider, value: _servicesDefine);

  /// Firebase is only a managed-profile capability. Keep this pure so
  /// notification/logger tests can exercise the boundary without initializing
  /// Firebase or reading mutable environment state.
  static bool allowsFor({
    required AppEnvironmentProfile profile,
    required bool configuredEnabled,
  }) {
    return configuredEnabled && profile != AppEnvironmentProfile.selfHosted;
  }

  /// Pure policy seam used by tests and by build-contract checks.
  static bool enabledForTesting({required String provider, required String value}) {
    final normalizedProvider = provider.trim().toLowerCase().replaceAll('-', '_');
    switch (value.trim().toLowerCase()) {
      case 'true':
        return true;
      case 'false':
        return false;
      case '':
        return normalizedProvider != 'better_auth';
      default:
        throw StateError('OMI_FIREBASE_SERVICES_ENABLED must be true or false when provided.');
    }
  }
}
