import 'package:flutter_test/flutter_test.dart';
import 'package:omi/env/environment_profile.dart';
import 'package:omi/utils/analytics/intercom.dart';

void main() {
  test('self-hosted support messenger rejects before invoking the managed SDK', () async {
    var sdkAccessorRead = false;

    final result = await IntercomManager.displayMessengerForDeployment<String>(
      profile: AppEnvironmentProfile.selfHosted,
      platformSupported: true,
      appId: 'inherited-managed-id',
      display: () async {
        // This closure is the production SDK accessor seam. If the profile
        // guard evaluates it, even without opening UI, the test fails.
        sdkAccessorRead = true;
        return 'opened';
      },
    );

    expect(result, isNull);
    expect(sdkAccessorRead, isFalse);
  });

  test('managed cloud profile retains support messenger behavior', () async {
    var invoked = false;

    final result = await IntercomManager.displayMessengerForDeployment<String>(
      profile: AppEnvironmentProfile.production,
      platformSupported: true,
      appId: 'managed-id',
      display: () async {
        invoked = true;
        return 'opened';
      },
    );

    expect(result, 'opened');
    expect(invoked, isTrue);
  });
}
