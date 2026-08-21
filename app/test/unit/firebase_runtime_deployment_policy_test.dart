import 'package:flutter_test/flutter_test.dart';
import 'package:talker_flutter/talker_flutter.dart';

import 'package:omi/env/environment_profile.dart';
import 'package:omi/env/firebase_services_policy.dart';
import 'package:omi/services/notifications/notification_service.dart';
import 'package:omi/services/firebase_background_runtime.dart';
import 'package:omi/utils/logger.dart';

class _FakeNotificationService implements NotificationInterface {
  _FakeNotificationService(this.deliveryCapability);

  @override
  final NotificationDeliveryCapability deliveryCapability;

  int permissionRequests = 0;
  int registrations = 0;
  int tokenSaves = 0;

  @override
  Future<bool> requestNotificationPermissions() async {
    permissionRequests += 1;
    return true;
  }

  @override
  Future<void> register() async {
    registrations += 1;
  }

  @override
  void saveNotificationToken() {
    tokenSaves += 1;
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _CountingObserver extends TalkerObserver {
  int errors = 0;
  int exceptions = 0;

  @override
  void onError(TalkerError err) {
    errors += 1;
  }

  @override
  void onException(TalkerException err) {
    exceptions += 1;
  }
}

void main() {
  test('self-hosted background delivery never initializes Firebase runtime', () async {
    var initialized = false;
    var handled = false;

    await runFirebaseBackgroundWorkIfEnabled(
      enabled: FirebaseServicesPolicy.allowsFor(
        profile: AppEnvironmentProfile.selfHosted,
        configuredEnabled: true,
      ),
      work: () async {
        initialized = true;
        handled = true;
      },
    );

    expect(initialized, isFalse);
    expect(handled, isFalse);
  });

  test('managed background delivery retains Firebase runtime behavior', () async {
    var initialized = false;
    var handled = false;

    await runFirebaseBackgroundWorkIfEnabled(
      enabled: FirebaseServicesPolicy.allowsFor(
        profile: AppEnvironmentProfile.production,
        configuredEnabled: true,
      ),
      work: () async {
        initialized = true;
        handled = true;
      },
    );

    expect(initialized, isTrue);
    expect(handled, isTrue);
  });

  test('self-hosted notification factory never evaluates the Firebase Messaging factory', () {
    var firebaseMessagingSdkConstructions = 0;
    var localProviderConstructions = 0;

    final provider = NotificationService.createForDeployment<String>(
      profile: AppEnvironmentProfile.selfHosted,
      firebaseServicesEnabled: true,
      createFirebaseProvider: () {
        firebaseMessagingSdkConstructions += 1;
        return 'firebase';
      },
      createNonFirebaseProvider: () {
        localProviderConstructions += 1;
        return 'local-only';
      },
    );

    expect(provider, 'local-only');
    expect(firebaseMessagingSdkConstructions, 0);
    expect(localProviderConstructions, 1);
    expect(
      FirebaseServicesPolicy.allowsFor(
        profile: AppEnvironmentProfile.selfHosted,
        configuredEnabled: true,
      ),
      isFalse,
    );
  });

  test('signed-in shell and home skip remote token work for a local-only provider', () async {
    final provider = _FakeNotificationService(NotificationDeliveryCapability.localOnly);

    final shellResult = provider.saveRemoteNotificationTokenIfSupported();
    final homeResult = await provider.registerRemoteNotificationsIfSupported();

    expect(shellResult, RemoteNotificationActionResult.unsupported);
    expect(homeResult, RemoteNotificationActionResult.unsupported);
    expect(provider.registrations, 0);
    expect(provider.tokenSaves, 0);
  });

  test('managed notification provider retains permission, registration, and token behavior', () async {
    var firebaseProviderConstructions = 0;
    final provider = NotificationService.createForDeployment<_FakeNotificationService>(
      profile: AppEnvironmentProfile.production,
      firebaseServicesEnabled: true,
      createFirebaseProvider: () {
        firebaseProviderConstructions += 1;
        return _FakeNotificationService(NotificationDeliveryCapability.firebaseRemote);
      },
      createNonFirebaseProvider: () => _FakeNotificationService(NotificationDeliveryCapability.localOnly),
    );

    expect(await provider.requestNotificationPermissionsIfSupported(), isTrue);
    expect(await provider.registerRemoteNotificationsIfSupported(), RemoteNotificationActionResult.completed);
    expect(provider.saveRemoteNotificationTokenIfSupported(), RemoteNotificationActionResult.completed);
    expect(firebaseProviderConstructions, 1);
    expect(provider.permissionRequests, 1);
    expect(provider.registrations, 1);
    expect(provider.tokenSaves, 2);
  });

  test('disabled onboarding provider does not request permission or register a token', () async {
    final provider = _FakeNotificationService(NotificationDeliveryCapability.disabled);

    expect(await provider.requestNotificationPermissionsIfSupported(), isFalse);
    expect(await provider.registerRemoteNotificationsIfSupported(), RemoteNotificationActionResult.unsupported);
    expect(provider.permissionRequests, 0);
    expect(provider.registrations, 0);
    expect(provider.tokenSaves, 0);
  });

  test('self-hosted logger error path never constructs a Crashlytics observer', () {
    var crashlyticsSdkConstructions = 0;
    final logger = Logger.forTesting(
      profile: AppEnvironmentProfile.selfHosted,
      firebaseServicesEnabled: true,
      createCrashObserver: () {
        crashlyticsSdkConstructions += 1;
        return _CountingObserver();
      },
    );

    logger.talker.handle(StateError('self-hosted failure'), StackTrace.current, 'failure');
    logger.talker.handle(Exception('self-hosted exception'), StackTrace.current, 'failure');

    expect(crashlyticsSdkConstructions, 0);
  });

  test('managed logger retains its Crashlytics observer behavior', () {
    var crashlyticsSdkConstructions = 0;
    final observer = _CountingObserver();
    final logger = Logger.forTesting(
      profile: AppEnvironmentProfile.production,
      firebaseServicesEnabled: true,
      createCrashObserver: () {
        crashlyticsSdkConstructions += 1;
        return observer;
      },
    );

    logger.talker.handle(StateError('managed failure'), StackTrace.current, 'failure');
    logger.talker.handle(Exception('managed exception'), StackTrace.current, 'failure');

    expect(crashlyticsSdkConstructions, 1);
    expect(observer.errors, 1);
    expect(observer.exceptions, 1);
  });
}
