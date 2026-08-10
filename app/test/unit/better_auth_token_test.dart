import 'package:flutter_test/flutter_test.dart';
import 'package:omi/providers/auth_provider.dart';

void main() {
  group('parseBetterAuthToken', () {
    test('returns the token from a valid /auth-issue response', () {
      expect(
        parseBetterAuthToken({'token': 'eyJhbGciOiJFUzI1NiJ9.payload.signature'}),
        'eyJhbGciOiJFUzI1NiJ9.payload.signature',
      );
    });

    test('returns null when token is missing', () {
      expect(parseBetterAuthToken({}), isNull);
    });

    test('returns null when token is empty', () {
      expect(parseBetterAuthToken({'token': ''}), isNull);
    });

    test('returns null when token is not a string', () {
      expect(parseBetterAuthToken({'token': 123}), isNull);
    });
  });
}
