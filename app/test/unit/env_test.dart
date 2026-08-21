import 'package:flutter_test/flutter_test.dart';
import 'package:omi/env/env.dart';
import 'package:omi/env/environment_profile.dart';
import 'package:omi/flavors.dart';
import 'package:omi/startup_routing.dart';
import 'dart:io';

/// Minimal EnvFields stub for testing Env logic in isolation.
/// Since Env._instance is late final (can only be set once per process),
/// we test with a single init and exercise the override/flag mechanisms.
class _TestEnvFields implements EnvFields {
  @override
  String? get openAIAPIKey => null;
  @override
  String? get posthogApiKey => null;
  @override
  String? get apiBaseUrl => null;
  @override
  String? get googleMapsApiKey => null;
  @override
  String? get intercomAppId => null;
  @override
  String? get intercomIOSApiKey => null;
  @override
  String? get intercomAndroidApiKey => null;
  @override
  String? get googleClientId => null;
  @override
  String? get googleClientSecret => null;
  @override
  bool? get useWebAuth => false;
  @override
  bool? get useAuthCustomToken => false;
}

void main() {
  // Init once for the entire test suite (late final constraint)
  setUpAll(() {
    Env.init(_TestEnvFields());
  });

  group('Env.isTestFlight', () {
    test('can be set to false', () {
      Env.isTestFlight = false;
      expect(Env.isTestFlight, isFalse);
    });

    test('can be set to true', () {
      Env.isTestFlight = true;
      expect(Env.isTestFlight, isTrue);
      // Clean up
      Env.isTestFlight = false;
    });
  });

  group('mobile environment profiles', () {
    test('local development is emulator-first and does not allow production data', () {
      expect(AppEnvironmentProfile.localDev.defaultApiBaseUrl, 'http://127.0.0.1:8000/');
      expect(AppEnvironmentProfile.localDev.firebaseProjectId, 'demo-omi-local');
      expect(AppEnvironmentProfile.localDev.usesFirebaseAuthEmulator, isTrue);
      expect(AppEnvironmentProfile.localDev.allowsProductionData, isFalse);
    });

    test('mobile beta explicitly pairs production Firebase with the dev serving plane', () {
      expect(AppEnvironmentProfile.mobileBeta.defaultApiBaseUrl, 'https://api.omiapi.com/');
      expect(AppEnvironmentProfile.mobileBeta.firebaseProjectId, 'based-hardware');
      expect(AppEnvironmentProfile.mobileBeta.usesFirebaseAuthEmulator, isFalse);
      expect(AppEnvironmentProfile.mobileBeta.allowsProductionData, isTrue);
      expect(AppEnvironmentProfile.mobileBeta.authCallbackScheme, 'omi-beta');
    });

    test('self-hosted profile has no managed Firebase identity or API default', () {
      expect(AppEnvironmentProfile.selfHosted.defaultApiBaseUrl, isEmpty);
      expect(AppEnvironmentProfile.selfHosted.firebaseProjectId, isEmpty);
      expect(AppEnvironmentProfile.selfHosted.managedClientValue('managed-secret'), isNull);
    });

    test('mobile beta keeps OAuth on the production identity plane', () {
      expect(
        Env.authApiBaseUrlForProfile(AppEnvironmentProfile.mobileBeta, servingApiBaseUrl: 'https://api.omiapi.com/'),
        Env.productionApiBaseUrl,
      );
    });

    test('local profile rejects a production Firebase project', () {
      expect(
        () =>
            Env.validateFirebaseProject(projectId: 'based-hardware', configuredProfile: AppEnvironmentProfile.localDev),
        throwsStateError,
      );
    });

    test('flavor defaults map to production and local profiles', () {
      expect(AppEnvironmentProfile.forFlavor(productionFlavor: true), AppEnvironmentProfile.production);
      expect(AppEnvironmentProfile.forFlavor(productionFlavor: false), AppEnvironmentProfile.localDev);
    });
  });

  group('Env.apiBaseUrl', () {
    test('uses the local emulator API when development env has no URL', () {
      expect(Env.apiBaseUrl, 'http://127.0.0.1:8000/');
    });

    test('returns override when set', () {
      Env.overrideApiBaseUrl('https://override.example.com/');
      expect(Env.apiBaseUrl, 'https://override.example.com/');
      Env.clearApiBaseUrlOverrideForTesting();
    });

    test('TestFlight production startup accepts the production API and WebSocket', () {
      validateApplicationStartupRouting(environment: Environment.prod, configuredApiBaseUrl: 'https://api.omi.me/');
      expect(Env.productionAgentProxyWsUrl, 'wss://agent.omi.me/v1/agent/ws');
    });

    test('Android production startup accepts the production API and WebSocket', () {
      validateApplicationStartupRouting(environment: Environment.prod, configuredApiBaseUrl: 'https://api.omi.me/');
      expect(Env.productionAgentProxyWsUrl, 'wss://agent.omi.me/v1/agent/ws');
    });

    test('mobile beta accepts the dev serving plane with production identity', () {
      Env.validateStartupRouting(
        productionFamily: true,
        configuredProfile: AppEnvironmentProfile.mobileBeta,
        configuredApiBaseUrl: 'https://api.omiapi.com/',
      );
    });

    test('production startup rejects legacy Beta, dev, staging, and arbitrary endpoints', () {
      for (final endpoint in [
        'https://api-beta.omi.me/',
        'https://api.omi.dev/',
        'https://staging.example.test/',
        'https://arbitrary.example.test/',
      ]) {
        expect(
          () => validateApplicationStartupRouting(environment: Environment.prod, configuredApiBaseUrl: endpoint),
          throwsStateError,
          reason: endpoint,
        );
      }
    });

    test('local development startup accepts the emulator API', () {
      expect(
        () => validateApplicationStartupRouting(
          environment: Environment.dev,
          configuredApiBaseUrl: 'http://127.0.0.1:8000/',
        ),
        returnsNormally,
      );
    });

    test('local development rejects the remote dev serving plane', () {
      expect(
        () => validateApplicationStartupRouting(
          environment: Environment.dev,
          configuredApiBaseUrl: 'https://api.omiapi.com/',
        ),
        throwsStateError,
      );
    });

    test('self-hosted routing requires an explicit non-Omi HTTPS origin', () {
      expect(
        () => Env.validateStartupRouting(
          productionFamily: true,
          configuredProfile: AppEnvironmentProfile.selfHosted,
          configuredApiBaseUrl: 'https://api.example.com/',
          releaseBuild: true,
        ),
        returnsNormally,
      );
      for (final endpoint in [
        '',
        'http://api.example.com',
        'https://api.omi.me',
        'https://user:secret@api.example.com',
        'https://api.example.com/path',
        'https://api.example.com/?query=value',
      ]) {
        expect(
          () => Env.validateStartupRouting(
            productionFamily: true,
            configuredProfile: AppEnvironmentProfile.selfHosted,
            configuredApiBaseUrl: endpoint,
            releaseBuild: true,
          ),
          throwsStateError,
          reason: endpoint,
        );
      }
      expect(
        Env.canonicalSelfHostedOrigin('HTTPS://API.Example.COM:443/', key: 'OMI_API_BASE_URL'),
        'https://api.example.com',
      );
    });

    test('self-hosted public origins require explicit operator HTTPS URLs', () {
      expect(
        () => Env.validateClientPublicOrigins(
          configuredProfile: AppEnvironmentProfile.selfHosted,
          configuredPrivacyUrl: 'https://docs.example.com/privacy',
          configuredTermsUrl: 'https://docs.example.com/terms',
          configuredShareUrl: 'https://share.example.com',
        ),
        returnsNormally,
      );
      expect(
        () => Env.validateClientPublicOrigins(
          configuredProfile: AppEnvironmentProfile.selfHosted,
          configuredPrivacyUrl: 'https://www.omi.me/privacy',
          configuredTermsUrl: 'https://docs.example.com/terms',
          configuredShareUrl: 'https://share.example.com',
        ),
        throwsStateError,
      );
    });
  });

  test('main invokes the production startup routing seam before services initialize', () {
    // Static wiring tripwire: the behavioral cases above call the exact seam.
    final mainSource = File('lib/main.dart').readAsStringSync();
    expect(mainSource, contains('validateApplicationStartupRouting(releaseBuild: kReleaseMode);'));
    expect(
      mainSource.indexOf('validateApplicationStartupRouting(releaseBuild: kReleaseMode);'),
      lessThan(mainSource.indexOf('ServiceManager.init()')),
    );
    expect(mainSource, contains('Env.validateFirebaseProject(projectId: Firebase.app().options.projectId);'));
  });
}
