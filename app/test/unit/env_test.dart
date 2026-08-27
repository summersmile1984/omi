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
  group('Android flavor trust plane', () {
    test('selfhost is a production-family flavor', () {
      expect(Environment.fromFlavorName('selfhost'), Environment.prod);
    });

    test('unknown flavors remain fail-safe development builds', () {
      expect(Environment.fromFlavorName('unexpected'), Environment.dev);
    });
  });

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

  test('agent proxy test override is declared and can be cleared', () {
    Env.overrideAgentProxyWsUrl('ws://agent.example.test/v1/agent/ws');
    expect(Env.agentProxyWsUrl, 'ws://agent.example.test/v1/agent/ws');
    Env.clearAgentProxyWsUrlOverrideForTesting();
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

    test('local prod pairs production Firebase with a developer-chosen backend', () {
      expect(AppEnvironmentProfile.localProd.firebaseProjectId, 'based-hardware');
      expect(AppEnvironmentProfile.localProd.usesFirebaseAuthEmulator, isFalse);
      expect(AppEnvironmentProfile.localProd.allowsProductionData, isTrue);
      expect(AppEnvironmentProfile.localProd.authCallbackScheme, 'omi');
    });

    test('self-hosted profile has no Firebase or Omi endpoint default', () {
      expect(AppEnvironmentProfile.selfHosted.defaultApiBaseUrl, isEmpty);
      expect(AppEnvironmentProfile.selfHosted.firebaseProjectId, isEmpty);
      expect(AppEnvironmentProfile.selfHosted.usesFirebaseAuthEmulator, isFalse);
      expect(AppEnvironmentProfile.selfHosted.managedClientValue('managed-secret'), isNull);
      expect(AppEnvironmentProfile.production.managedClientValue('managed-secret'), 'managed-secret');
    });

    test('self-hosted app instructions never fall back to managed GitHub content', () {
      const managedPath = 'https://raw.githubusercontent.com/BasedHardware/Omi/main/plugins/instructions/a/README.md';
      expect(Env.isManagedAppInstructionsPath(managedPath), isTrue);
      expect(
        Env.supportsAppInstructions(raw: managedPath, configuredProfile: AppEnvironmentProfile.selfHosted),
        isFalse,
      );
      expect(
        Env.supportsAppInstructions(raw: managedPath, configuredProfile: AppEnvironmentProfile.production),
        isTrue,
      );
    });

    test('self-hosted clients cannot download models from managed origins', () {
      expect(
        Env.allowsManagedModelDownloads(configuredProfile: AppEnvironmentProfile.selfHosted),
        isFalse,
      );
      expect(
        Env.allowsManagedModelDownloads(configuredProfile: AppEnvironmentProfile.production),
        isTrue,
      );
    });

    test('self-hosted firmware downloads require an operator HTTPS origin', () {
      expect(
        Env.validateFirmwareDownloadUrl(
          'https://objects.example.com/firmware/Omi_CV1.zip?sig=operator',
          configuredProfile: AppEnvironmentProfile.selfHosted,
        ),
        'https://objects.example.com/firmware/Omi_CV1.zip?sig=operator',
      );
      for (final invalid in [
        'http://objects.example.com/firmware/Omi_CV1.zip',
        'https://api.omi.me/releases/Omi_CV1.zip',
        'https://github.com/BasedHardware/omi/releases/download/fw.zip',
        'https://objects.githubusercontent.com/omi/fw.zip',
        'https://user:secret@objects.example.com/firmware/Omi_CV1.zip',
        'https://objects.example.com/firmware/Omi_CV1.zip#fragment',
        'not-a-url',
      ]) {
        expect(
          () => Env.validateFirmwareDownloadUrl(invalid, configuredProfile: AppEnvironmentProfile.selfHosted),
          throwsStateError,
          reason: invalid,
        );
      }
      expect(
        Env.validateFirmwareDownloadUrl(
          'https://github.com/BasedHardware/omi/releases/download/fw.zip',
          configuredProfile: AppEnvironmentProfile.production,
        ),
        'https://github.com/BasedHardware/omi/releases/download/fw.zip',
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

    test('TestFlight production startup accepts the production API', () {
      validateApplicationStartupRouting(environment: Environment.prod, configuredApiBaseUrl: 'https://api.omi.me/');
    });

    test('Android production startup accepts the production API', () {
      validateApplicationStartupRouting(environment: Environment.prod, configuredApiBaseUrl: 'https://api.omi.me/');
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

    test('local dev accepts loopback and every private-network range, including CGNAT', () {
      for (final endpoint in [
        'http://127.0.0.1:8000/',
        'http://localhost:8000/',
        'http://10.0.0.5:8000/',
        'http://172.16.0.5:8000/',
        'http://172.31.255.254:8000/',
        'http://192.168.1.20:8000/',
        // 100.64.0.0/10 (RFC 6598, carrier-grade NAT) is the range Tailscale
        // assigns. A physical device cannot reach the local harness any other
        // way — the harness binds loopback only by design — so rejecting this
        // range stranded the app on a blank splash with no diagnostic.
        'http://100.64.0.1:8000/',
        'http://100.105.2.5:8000/',
        'http://100.127.255.254:8000/',
      ]) {
        Env.validateStartupRouting(
          productionFamily: false,
          configuredProfile: AppEnvironmentProfile.localDev,
          configuredApiBaseUrl: endpoint,
        );
      }
    });

    test('local dev still rejects public endpoints and the edges just outside CGNAT', () {
      for (final endpoint in [
        'https://api.omi.me/',
        'https://api.omiapi.com/',
        // 100.63.x and 100.128.x sit immediately outside 100.64.0.0/10 and must
        // stay rejected — widening this must not degrade into "any 100.x host".
        'http://100.63.255.255:8000/',
        'http://100.128.0.1:8000/',
        'http://8.8.8.8:8000/',
      ]) {
        expect(
          () => Env.validateStartupRouting(
            productionFamily: false,
            configuredProfile: AppEnvironmentProfile.localDev,
            configuredApiBaseUrl: endpoint,
          ),
          throwsStateError,
          reason: endpoint,
        );
      }
    });

    test('local prod accepts loopback, private-network, and tunnel endpoints in debug builds', () {
      for (final endpoint in [
        'http://127.0.0.1:8000/',
        'http://192.168.1.20:8000/',
        'https://example.ngrok-free.app/',
      ]) {
        Env.validateStartupRouting(
          productionFamily: true,
          configuredProfile: AppEnvironmentProfile.localProd,
          configuredApiBaseUrl: endpoint,
          releaseBuild: false,
        );
      }
    });

    test('local prod is rejected in release builds', () {
      expect(
        () => Env.validateStartupRouting(
          productionFamily: true,
          configuredProfile: AppEnvironmentProfile.localProd,
          configuredApiBaseUrl: 'http://127.0.0.1:8000/',
          releaseBuild: true,
        ),
        throwsStateError,
      );
    });

    test('local prod rejects a malformed endpoint', () {
      expect(
        () => Env.validateStartupRouting(
          productionFamily: true,
          configuredProfile: AppEnvironmentProfile.localProd,
          configuredApiBaseUrl: 'not a url',
          releaseBuild: false,
        ),
        throwsStateError,
      );
    });

    test('self-hosted release accepts only an explicit non-Omi HTTPS API', () {
      Env.validateStartupRouting(
        productionFamily: true,
        configuredProfile: AppEnvironmentProfile.selfHosted,
        configuredApiBaseUrl: 'https://api.example.com/',
        releaseBuild: true,
      );
      for (final endpoint in ['http://api.example.com/', 'https://api.omi.me/', 'https://api.omiapi.com/', '']) {
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
      for (final endpoint in [
        'https://user:secret@api.example.com/',
        'https://api.example.com/path',
        'https://api.example.com/?query=value',
        'https://api.example.com/#fragment',
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
        Env.canonicalSelfHostedOrigin(
          'HTTPS://API.Example.COM:443/',
          key: 'OMI_API_BASE_URL',
        ),
        'https://api.example.com',
      );
    });

    test('self-hosted client public origins are explicit, HTTPS, and non-Omi', () {
      Env.validateClientPublicOrigins(
        configuredProfile: AppEnvironmentProfile.selfHosted,
        configuredPrivacyUrl: 'https://legal.example.com/privacy',
        configuredTermsUrl: 'https://legal.example.com/terms',
        configuredShareUrl: 'https://share.example.com',
        configuredMcpBaseUrl: 'https://mcp.example.com',
      );
      for (final invalid in ['', 'http://legal.example.com/privacy', 'https://www.omi.me/pages/privacy']) {
        expect(
          () => Env.validateClientPublicOrigins(
            configuredProfile: AppEnvironmentProfile.selfHosted,
            configuredPrivacyUrl: invalid,
            configuredTermsUrl: 'https://legal.example.com/terms',
            configuredShareUrl: 'https://share.example.com',
            configuredMcpBaseUrl: 'https://mcp.example.com',
          ),
          throwsStateError,
        );
      }
    });

    test('self-hosted MCP authority is explicit and distinct from the API origin', () {
      expect(
        Env.resolveMcpBaseUrl(
          configuredProfile: AppEnvironmentProfile.selfHosted,
          configuredMcpBaseUrl: 'HTTPS://MCP.Example.COM:443/',
          configuredApiBaseUrl: 'https://api.example.com/',
        ),
        'https://mcp.example.com/',
      );
      for (final invalid in ['', 'http://mcp.example.com', 'https://api.omi.me', 'https://mcp.example.com/root']) {
        expect(
          () => Env.resolveMcpBaseUrl(
            configuredProfile: AppEnvironmentProfile.selfHosted,
            configuredMcpBaseUrl: invalid,
            configuredApiBaseUrl: 'https://api.example.com/',
          ),
          throwsStateError,
        );
      }
    });

    test('self-hosted share origin never falls back to the managed service', () {
      expect(
        Env.resolveShareBaseUrl(
          configuredProfile: AppEnvironmentProfile.selfHosted,
          configuredShareUrl: 'HTTPS://SHARE.Example.COM:443/',
        ),
        'https://share.example.com',
      );
      for (final invalid in ['', 'http://share.example.com', 'https://h.omi.me', 'https://share.example.com/path']) {
        expect(
          () => Env.resolveShareBaseUrl(
            configuredProfile: AppEnvironmentProfile.selfHosted,
            configuredShareUrl: invalid,
          ),
          throwsStateError,
        );
      }
    });

    test('native capture configuration has no managed API fallback', () {
      expect(Env.requireConfiguredApiBaseUrl('https://api.example.com/'), 'https://api.example.com/');
      expect(() => Env.requireConfiguredApiBaseUrl(''), throwsStateError);
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
          configuredMcpBaseUrl: 'https://mcp.example.com',
        ),
        returnsNormally,
      );
      expect(
        () => Env.validateClientPublicOrigins(
          configuredProfile: AppEnvironmentProfile.selfHosted,
          configuredPrivacyUrl: 'https://www.omi.me/privacy',
          configuredTermsUrl: 'https://docs.example.com/terms',
          configuredShareUrl: 'https://share.example.com',
          configuredMcpBaseUrl: 'https://mcp.example.com',
        ),
        throwsStateError,
      );
    });

    test('self-hosted operator push registration is an explicit origin', () {
      expect(
        Env.operatorPushRegistrationBaseUrlForProfile(
          configuredProfile: AppEnvironmentProfile.selfHosted,
          configuredUrl: 'HTTPS://PUSH.Example.COM:443/',
        ),
        'https://push.example.com',
      );
      expect(
        Env.operatorPushRegistrationBaseUrlForProfile(
          configuredProfile: AppEnvironmentProfile.production,
          configuredUrl: 'https://push.example.com',
        ),
        isNull,
      );
      expect(
        Env.operatorPushRegistrationBaseUrlForProfile(
          configuredProfile: AppEnvironmentProfile.selfHosted,
          configuredUrl: null,
        ),
        isNull,
      );
      for (final invalid in [
        'http://push.example.com',
        'https://push.example.com/register',
        'https://push.example.com/?provider=webhook',
        'https://www.omi.me',
      ]) {
        expect(
          () => Env.operatorPushRegistrationBaseUrlForProfile(
            configuredProfile: AppEnvironmentProfile.selfHosted,
            configuredUrl: invalid,
          ),
          throwsStateError,
          reason: invalid,
        );
      }
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
    // Same guarantee as before — the project id of the Firebase app main.dart ends
    // up with is fed to Env.validateFirebaseProject — but the statement it used to
    // name verbatim now lives in ensureFirebaseApp() (lib/startup_firebase.dart),
    // which applies it to the freshly initialized app, the already-running native
    // app, and the app adopted after [core/duplicate-app] alike. So this pins the
    // wiring instead of one branch's text; that ensureFirebaseApp actually calls it
    // on every one of those paths is asserted behaviorally in
    // test/unit/startup_firebase_duplicate_app_test.dart.
    expect(
      mainSource,
      matches(
        RegExp(
          r'projectIdOf:\s*\(app\)\s*=>\s*app\.options\.projectId,\s*'
          r'validateProject:\s*\(projectId\)\s*=>\s*Env\.validateFirebaseProject\(projectId:\s*projectId\),',
        ),
      ),
    );
  });
}
