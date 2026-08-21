import 'package:omi/env/environment_profile.dart';

class OnboardingIdentity {
  const OnboardingIdentity({
    required this.uid,
    required this.email,
    required this.displayName,
    required this.updateManagedSupportProfile,
  });

  final String uid;
  final String email;
  final String displayName;
  final bool updateManagedSupportProfile;

  static bool allowsManagedSupport(AppEnvironmentProfile profile) => profile != AppEnvironmentProfile.selfHosted;

  factory OnboardingIdentity.fromCachedSession({
    required AppEnvironmentProfile profile,
    required String uid,
    required String email,
    required String displayName,
  }) {
    if (uid.isEmpty) throw StateError('Onboarding requires a cached authenticated identity.');
    return OnboardingIdentity(
      uid: uid,
      email: email,
      displayName: displayName,
      updateManagedSupportProfile: allowsManagedSupport(profile),
    );
  }
}
