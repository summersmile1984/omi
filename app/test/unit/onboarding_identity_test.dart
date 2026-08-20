import 'package:flutter_test/flutter_test.dart';
import 'package:omi/env/environment_profile.dart';
import 'package:omi/pages/onboarding/onboarding_identity.dart';

void main() {
  test('fresh Better Auth onboarding uses cached identity and disables managed support egress', () {
    final identity = OnboardingIdentity.fromCachedSession(
      profile: AppEnvironmentProfile.selfHosted,
      uid: 'better-auth-user',
      email: 'user@operator.example',
      displayName: 'Operator User',
    );

    expect(identity.uid, 'better-auth-user');
    expect(identity.updateManagedSupportProfile, isFalse);
    expect(OnboardingIdentity.allowsManagedSupport(AppEnvironmentProfile.selfHosted), isFalse);
  });

  test('managed cloud onboarding retains the support login flow', () {
    expect(OnboardingIdentity.allowsManagedSupport(AppEnvironmentProfile.production), isTrue);
  });

  test('onboarding fails closed without an authenticated cached identity', () {
    expect(
      () => OnboardingIdentity.fromCachedSession(
        profile: AppEnvironmentProfile.selfHosted,
        uid: '',
        email: '',
        displayName: '',
      ),
      throwsStateError,
    );
  });
}
