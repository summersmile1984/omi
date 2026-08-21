import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:omi/services/auth/better_auth_client.dart';

String _jwtWithExpiration(int expiration) {
  final header = base64Url.encode(utf8.encode(jsonEncode({'alg': 'ES256'}))).replaceAll('=', '');
  final payload = base64Url.encode(utf8.encode(jsonEncode({'sub': 'user-1', 'exp': expiration}))).replaceAll('=', '');
  return '$header.$payload.signature';
}

void main() {
  test('email sign-in exchanges bearer session for an API JWT', () async {
    final expiration = DateTime.now().toUtc().add(const Duration(minutes: 15)).millisecondsSinceEpoch ~/ 1000;
    final requests = <http.Request>[];
    final client = BetterAuthClient(
      baseUrl: 'https://auth.example/',
      httpClient: MockClient((request) async {
        requests.add(request);
        if (request.url.path == '/api/auth/sign-in/email') {
          return http.Response(
            jsonEncode({
              'user': {'id': 'user-1', 'email': 'person@example.com', 'name': 'Person Example'},
            }),
            200,
            headers: {'set-auth-token': 'signed-session-token'},
          );
        }
        if (request.url.path == '/api/auth/token') {
          return http.Response(jsonEncode({'token': _jwtWithExpiration(expiration)}), 200);
        }
        return http.Response('', 404);
      }),
    );

    final session = await client.signInEmail(email: 'person@example.com', password: 'correct-password');

    expect(session.uid, 'user-1');
    expect(session.sessionToken, 'signed-session-token');
    expect(session.expirationTime.millisecondsSinceEpoch, expiration * 1000);
    expect(requests.map((request) => request.url.path), ['/api/auth/sign-in/email', '/api/auth/token']);
    expect(requests.last.headers['Authorization'], 'Bearer signed-session-token');
  });

  test('sign-in fails closed when no bearer session is returned', () async {
    final client = BetterAuthClient(
      baseUrl: 'https://auth.example',
      httpClient: MockClient(
        (_) async => http.Response(
          jsonEncode({
            'user': {'id': 'user-1'},
          }),
          200,
        ),
      ),
    );

    await expectLater(
      client.signInEmail(email: 'person@example.com', password: 'correct-password'),
      throwsA(isA<BetterAuthException>().having((error) => error.code, 'code', 'missing_session_token')),
    );
  });

  test('expired session is a terminal refresh class', () async {
    final client = BetterAuthClient(
      baseUrl: 'https://auth.example',
      httpClient: MockClient((_) async => http.Response('', 401)),
    );

    await expectLater(
      client.refreshJwt('expired-session'),
      throwsA(isA<BetterAuthException>().having((error) => error.code, 'code', 'session_expired')),
    );
  });
}
