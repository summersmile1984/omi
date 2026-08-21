import 'package:omi/env/environment_profile.dart';

/// Runtime boundary for managed Firebase SDKs.
///
/// The build flag remains useful for official releases, but it is never
/// authoritative enough to enable Firebase in a self-hosted binary.
abstract final class FirebaseServicesPolicy {
  static const bool configuredEnabled = bool.fromEnvironment(
    'OMI_FIREBASE_SERVICES_ENABLED',
    defaultValue: true,
  );
  static const String _configuredProfile = String.fromEnvironment('OMI_APP_PROFILE');

  static bool allowsFor({
    required AppEnvironmentProfile profile,
    required bool configuredEnabled,
  }) {
    return configuredEnabled && profile != AppEnvironmentProfile.selfHosted;
  }

  /// This getter intentionally does not read [Env] or flavor state: logging is
  /// used while those globals initialize and must not create an init cycle.
  static bool get enabled => configuredEnabled && _configuredProfile != 'self_hosted';
}
