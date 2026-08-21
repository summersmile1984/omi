import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/auth/better_auth_client.dart';

void main() {
  test('JWT expiration parsing rejects tokens without an exp claim', () {
    expect(
      () => BetterAuthClient.jwtExpiration('eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEifQ.signature'),
      throwsA(isA<BetterAuthException>().having((error) => error.code, 'code', 'missing_jwt_expiration')),
    );
  });

  test('dev issuer is disabled without explicit compile-time configuration', () {
    final provider = AuthenticationProvider(initializeListeners: false);
    addTearDown(provider.dispose);

    expect(provider.betterAuthDevSignInEnabled, isFalse);
  });
}
