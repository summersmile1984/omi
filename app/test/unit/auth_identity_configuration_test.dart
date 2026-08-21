import 'package:flutter_test/flutter_test.dart';
import 'package:omi/env/environment_profile.dart';
import 'package:omi/services/auth_service.dart';

void main() {
  test('self-hosted provider linking rejects before any Firebase/OAuth entrypoint', () {
    expect(
      () => AuthService.ensureProviderLinkAllowed(AppEnvironmentProfile.selfHosted),
      throwsStateError,
    );
  });

  test('Better Auth production configuration is explicit and Firebase-free', () {
    expect(
      () => AuthService.validateIdentityConfiguration(
        configuredProvider: 'better_auth',
        configuredServerUrl: 'https://auth.example.com',
        configuredProfile: AppEnvironmentProfile.selfHosted,
        releaseMode: true,
        firebaseServicesEnabled: false,
      ),
      returnsNormally,
    );

    for (final invalid in <({String provider, String url, bool firebaseEnabled})>[
      (provider: 'better_auth', url: '', firebaseEnabled: false),
      (provider: 'better_auth', url: 'http://auth.example.com', firebaseEnabled: false),
      (provider: 'better_auth', url: 'https://auth.example.com', firebaseEnabled: true),
      (provider: 'firebase', url: '', firebaseEnabled: false),
      (provider: 'firebase', url: '', firebaseEnabled: true),
      (provider: 'unknown', url: 'https://auth.example.com', firebaseEnabled: false),
      (provider: 'better_auth', url: 'https://auth.omi.me', firebaseEnabled: false),
      (provider: 'better_auth', url: 'https://user:secret@auth.example.com', firebaseEnabled: false),
      (provider: 'better_auth', url: 'https://auth.example.com/path', firebaseEnabled: false),
      (provider: 'better_auth', url: 'https://auth.example.com?query=value', firebaseEnabled: false),
      (provider: 'better_auth', url: 'https://auth.example.com#fragment', firebaseEnabled: false),
    ]) {
      expect(
        () => AuthService.validateIdentityConfiguration(
          configuredProvider: invalid.provider,
          configuredServerUrl: invalid.url,
          configuredProfile: AppEnvironmentProfile.selfHosted,
          releaseMode: true,
          firebaseServicesEnabled: invalid.firebaseEnabled,
        ),
        throwsStateError,
        reason: invalid.toString(),
      );
    }
  });

  test('Better Auth local development may use an explicit HTTP origin', () {
    expect(
      () => AuthService.validateIdentityConfiguration(
        configuredProvider: 'better-auth',
        configuredServerUrl: 'http://127.0.0.1:3000',
        configuredProfile: AppEnvironmentProfile.localDev,
        releaseMode: false,
        firebaseServicesEnabled: false,
      ),
      returnsNormally,
    );
  });
}
