import 'package:flutter_test/flutter_test.dart';
import 'package:omi/providers/auth_provider.dart';

void main() {
  group('parseBetterAuthDevCredential', () {
    test('returns the token and matching uid from a valid response', () {
      expect(
        parseBetterAuthDevCredential({'token': 'eyJhbGciOiJFUzI1NiJ9.payload.signature', 'uid': 'mobile-better-auth'}),
        (token: 'eyJhbGciOiJFUzI1NiJ9.payload.signature', uid: 'mobile-better-auth'),
      );
    });

    test('returns null when token is missing', () {
      expect(parseBetterAuthDevCredential({'uid': 'mobile-better-auth'}), isNull);
    });

    test('returns null when uid is missing', () {
      expect(parseBetterAuthDevCredential({'token': 'token'}), isNull);
    });

    test('returns null when either field is empty', () {
      expect(parseBetterAuthDevCredential({'token': '', 'uid': 'mobile-better-auth'}), isNull);
      expect(parseBetterAuthDevCredential({'token': 'token', 'uid': ''}), isNull);
    });
  });

  test('dev issuer is disabled without explicit compile-time configuration', () {
    final provider = AuthenticationProvider(initializeListeners: false);
    addTearDown(provider.dispose);

    expect(provider.betterAuthDevSignInEnabled, isFalse);
  });
}
