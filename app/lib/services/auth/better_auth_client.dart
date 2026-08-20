import 'dart:convert';

import 'package:http/http.dart' as http;

final class BetterAuthException implements Exception {
  const BetterAuthException(this.code, {this.statusCode});

  final String code;
  final int? statusCode;

  @override
  String toString() => 'BetterAuthException($code)';
}

final class BetterAuthSession {
  const BetterAuthSession({
    required this.sessionToken,
    required this.jwt,
    required this.uid,
    required this.expirationTime,
    this.email,
    this.displayName,
  });

  final String sessionToken;
  final String jwt;
  final String uid;
  final DateTime expirationTime;
  final String? email;
  final String? displayName;
}

/// Native/mobile Better Auth client using the signed bearer-session plugin.
///
/// Browser cookies are deliberately not required: sign-in returns a
/// `set-auth-token` session credential, which is exchanged at `/token` for the
/// short-lived JWT sent to the Python API.
final class BetterAuthClient {
  BetterAuthClient({required String baseUrl, http.Client? httpClient})
      : _baseUrl = baseUrl.replaceFirst(RegExp(r'/+$'), ''),
        _httpClient = httpClient ?? http.Client();

  final String _baseUrl;
  final http.Client _httpClient;

  Future<BetterAuthSession> signInEmail({required String email, required String password}) async {
    return _establishSession('/api/auth/sign-in/email', {
      'email': email.trim(),
      'password': password,
      'rememberMe': true,
    });
  }

  Future<BetterAuthSession> signUpEmail({required String email, required String password, required String name}) async {
    return _establishSession('/api/auth/sign-up/email', {
      'email': email.trim(),
      'password': password,
      'name': name.trim(),
      'rememberMe': true,
    });
  }

  Future<BetterAuthSession> _establishSession(String path, Map<String, dynamic> body) async {
    final response = await _httpClient.post(
      Uri.parse('$_baseUrl$path'),
      headers: const {'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: jsonEncode(body),
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw BetterAuthException('sign_in_failed', statusCode: response.statusCode);
    }
    final sessionToken = response.headers['set-auth-token'];
    if (sessionToken == null || sessionToken.isEmpty) {
      throw const BetterAuthException('missing_session_token');
    }
    final payload = _object(response.body);
    final user = payload['user'];
    if (user is! Map<String, dynamic>) throw const BetterAuthException('missing_user');
    final uid = user['id'];
    if (uid is! String || uid.isEmpty) throw const BetterAuthException('missing_user');
    final jwt = await refreshJwt(sessionToken);
    return BetterAuthSession(
      sessionToken: sessionToken,
      jwt: jwt.token,
      uid: uid,
      expirationTime: jwt.expirationTime,
      email: user['email'] is String ? user['email'] as String : null,
      displayName: user['name'] is String ? user['name'] as String : null,
    );
  }

  Future<({String token, DateTime expirationTime})> refreshJwt(String sessionToken) async {
    if (sessionToken.isEmpty) throw const BetterAuthException('missing_session_token');
    final response = await _httpClient.get(
      Uri.parse('$_baseUrl/api/auth/token'),
      headers: {'Authorization': 'Bearer $sessionToken', 'Accept': 'application/json'},
    );
    if (response.statusCode == 401 || response.statusCode == 403) {
      throw BetterAuthException('session_expired', statusCode: response.statusCode);
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw BetterAuthException('token_refresh_failed', statusCode: response.statusCode);
    }
    final token = _object(response.body)['token'];
    if (token is! String || token.isEmpty) throw const BetterAuthException('missing_jwt');
    return (token: token, expirationTime: jwtExpiration(token));
  }

  Future<void> signOut(String sessionToken) async {
    if (sessionToken.isEmpty) return;
    final response = await _httpClient.post(
      Uri.parse('$_baseUrl/api/auth/sign-out'),
      headers: {'Authorization': 'Bearer $sessionToken', 'Accept': 'application/json'},
    );
    if (response.statusCode >= 500) {
      throw BetterAuthException('sign_out_failed', statusCode: response.statusCode);
    }
  }

  static DateTime jwtExpiration(String token) {
    final parts = token.split('.');
    if (parts.length != 3) throw const BetterAuthException('invalid_jwt');
    try {
      final payload = jsonDecode(utf8.decode(base64Url.decode(base64Url.normalize(parts[1]))));
      final expiration = payload is Map<String, dynamic> ? payload['exp'] : null;
      if (expiration is! num) throw const BetterAuthException('missing_jwt_expiration');
      return DateTime.fromMillisecondsSinceEpoch(expiration.toInt() * 1000, isUtc: true);
    } on BetterAuthException {
      rethrow;
    } catch (_) {
      throw const BetterAuthException('invalid_jwt');
    }
  }

  static Map<String, dynamic> _object(String body) {
    try {
      final decoded = jsonDecode(body);
      if (decoded is Map<String, dynamic>) return decoded;
    } catch (_) {
      // Public error classification is intentionally stable and body-agnostic.
    }
    throw const BetterAuthException('invalid_response');
  }
}
