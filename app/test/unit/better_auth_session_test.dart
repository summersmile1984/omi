import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/auth/better_auth_session.dart';

void main() {
  final now = DateTime.utc(2026, 8, 28, 12);

  test('accepts a non-empty session with more than the safety window left', () {
    expect(
      isBetterAuthSessionUsable(
        token: 'jwt',
        uid: 'mobile-user',
        expirationTime: now.add(const Duration(minutes: 6)).millisecondsSinceEpoch,
        now: now,
      ),
      isTrue,
    );
  });

  test('rejects missing identity, token, or an expiring session', () {
    final expirationTime = now.add(const Duration(minutes: 6)).millisecondsSinceEpoch;
    expect(isBetterAuthSessionUsable(token: '', uid: 'mobile-user', expirationTime: expirationTime, now: now), isFalse);
    expect(isBetterAuthSessionUsable(token: 'jwt', uid: '', expirationTime: expirationTime, now: now), isFalse);
    expect(isBetterAuthSessionUsable(token: 'jwt', uid: 'mobile-user', expirationTime: 0, now: now), isFalse);
    expect(
      isBetterAuthSessionUsable(
        token: 'jwt',
        uid: 'mobile-user',
        expirationTime: now.add(const Duration(minutes: 5)).millisecondsSinceEpoch,
        now: now,
      ),
      isFalse,
    );
  });
}
