import 'package:flutter_test/flutter_test.dart';
import 'package:omi/env/firebase_services_policy.dart';
import 'package:omi/utils/debugging/crashlytics_manager.dart';

void main() {
  test('Better Auth defaults to a Firebase-free runtime', () {
    expect(
      FirebaseServicesPolicy.enabledForTesting(provider: 'better-auth', value: ''),
      isFalse,
    );
  });

  test('self-hosted profile disables Firebase before provider validation', () {
    expect(
      FirebaseServicesPolicy.enabledForTesting(
        profile: 'self_hosted',
        provider: 'firebase',
        value: 'true',
      ),
      isFalse,
    );
  });

  test('Crashlytics requires an initialized Firebase app', () {
    expect(
      FirebaseServicesPolicy.allowsCrashReporting(servicesEnabled: true, hasInitializedApp: false),
      isFalse,
    );
    expect(
      FirebaseServicesPolicy.allowsCrashReporting(servicesEnabled: true, hasInitializedApp: true),
      isTrue,
    );
    expect(
      FirebaseServicesPolicy.allowsCrashReporting(servicesEnabled: false, hasInitializedApp: true),
      isFalse,
    );
  });

  test('managed Firebase remains opt-in explicit and invalid values fail closed', () {
    expect(FirebaseServicesPolicy.enabledForTesting(provider: 'firebase', value: 'true'), isTrue);
    expect(FirebaseServicesPolicy.enabledForTesting(provider: 'firebase', value: 'false'), isFalse);
    expect(
      () => FirebaseServicesPolicy.enabledForTesting(provider: 'firebase', value: 'maybe'),
      throwsStateError,
    );
    expect(
      FirebaseServicesPolicy.enabledForTesting(provider: 'unexpected-provider', value: 'true'),
      isFalse,
    );
  });

  test('Crashlytics manager is inert before Firebase is configured', () async {
    await CrashlyticsManager.init();
    CrashlyticsManager.instance.identifyUser('email', 'name', 'uid');
    CrashlyticsManager.instance.logError('self-hosted test error');
    await CrashlyticsManager.instance.reportCrash(Exception('test'), StackTrace.current);
  });
}
