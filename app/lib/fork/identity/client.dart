import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:omi/services/auth/auth_token_result.dart';
import 'credential.dart';
import 'deployment.dart';

typedef IdentityTransport = Future<http.Response> Function(http.Request request);

/// Protocol transport only. The session owner serializes durable mutations and
/// rejects superseded completions. JWTs are never accepted as login sessions.
final class IdentityClient {
  IdentityClient({required this.deployment, IdentityTransport? transport, DateTime Function()? now})
      : _transport = transport ?? _live,
        _now = now ?? DateTime.now;
  final NativeDeployment deployment;
  final IdentityTransport _transport;
  final DateTime Function() _now;
  static Future<http.Response> _live(http.Request request) async {
    final client = http.Client();
    try {
      return await (() async => http.Response.fromStream(await client.send(request)))()
          .timeout(const Duration(seconds: 12));
    } finally {
      client.close();
    }
  }

  Future<http.Response> _request(String path, {String? session, Map<String, String>? body}) async {
    final request = http.Request(body == null ? 'GET' : 'POST', deployment.authEndpoint(path))..followRedirects = false;
    if (session != null) request.headers['Authorization'] = 'Bearer $session';
    if (body != null) {
      request.headers['Content-Type'] = 'application/json';
      request.body = jsonEncode(body);
    }
    final http.Response response;
    try {
      response = await _transport(request).timeout(const Duration(seconds: 12));
    } catch (_) {
      throw const IdentityException(IdentityFailure.unavailable);
    }
    if (response.statusCode == 401) throw const IdentityException(IdentityFailure.unauthorized);
    if (response.statusCode >= 500) throw const IdentityException(IdentityFailure.unavailable);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw const IdentityException(IdentityFailure.rejected);
    }
    return response;
  }

  Future<OpaqueCredential> authenticate({required String email, required String password, String? name}) async {
    final http.Response response;
    try {
      response = await _request(name == null ? 'sign-in/email' : 'sign-up/email',
          body: {'email': email, 'password': password, if (name != null) 'name': name});
    } on IdentityException catch (error) {
      if (error.failure == IdentityFailure.unauthorized) throw const IdentityException(IdentityFailure.rejected);
      rethrow;
    }
    try {
      final token = response.headers['set-auth-token'];
      if (token == null || token.isEmpty) throw const FormatException('Missing opaque credential');
      final value = jsonDecode(response.body) as Map<String, dynamic>;
      return OpaqueCredential(session: token, user: OpaqueCredential.parseUser(value['user']));
    } catch (_) {
      throw const IdentityException(IdentityFailure.invalidResponse);
    }
  }

  Future<OpaqueCredential> restore(OpaqueCredential credential) async {
    final response = await _request('get-session', session: credential.session);
    final dynamic value;
    try {
      value = jsonDecode(response.body);
    } catch (_) {
      throw const IdentityException(IdentityFailure.invalidResponse);
    }
    if (value == null) throw const IdentityException(IdentityFailure.unauthorized);
    try {
      final user = OpaqueCredential.parseUser((value as Map<String, dynamic>)['user']);
      if (user.uid != credential.user.uid) throw const IdentityException(IdentityFailure.unauthorized);
      return OpaqueCredential(session: credential.session, user: user);
    } on IdentityException {
      rethrow;
    } catch (_) {
      throw const IdentityException(IdentityFailure.invalidResponse);
    }
  }

  Future<RefreshedAuthToken> refresh(OpaqueCredential credential) async {
    final response = await _request('token', session: credential.session);
    try {
      final token = (jsonDecode(response.body) as Map<String, dynamic>)['token'] as String;
      final parts = token.split('.');
      if (parts.length != 3) throw const FormatException('Invalid JWT');
      // Local cache admission allows 60 seconds of issued-at clock skew.
      // See contracts/auth/client-cache-admission.md. API/WS owners verify signatures, issuer,
      // audience, publication grace and live session revocation on every call.
      final claims = jsonDecode(utf8.decode(base64Url.decode(base64Url.normalize(parts[1])))) as Map<String, dynamic>;
      final iat = claims['iat'], exp = claims['exp'];
      final now = _now().millisecondsSinceEpoch ~/ 1000;
      if (iat is! int ||
          exp is! int ||
          iat < 0 ||
          iat > now + 60 ||
          exp <= now ||
          exp <= iat ||
          exp - iat > 3600 ||
          claims['sub'] != credential.user.uid ||
          claims['uid'] != credential.user.uid ||
          claims['sid'] is! String ||
          (claims['sid'] as String).isEmpty) {
        throw const FormatException('Invalid JWT lifetime or owner');
      }
      return RefreshedAuthToken(
          token: token, expirationTime: DateTime.fromMillisecondsSinceEpoch(exp * 1000, isUtc: true));
    } catch (_) {
      throw const IdentityException(IdentityFailure.invalidResponse);
    }
  }

  Future<OpaqueCredential> updateName(OpaqueCredential credential, String name) async {
    await _request('update-user', session: credential.session, body: {'name': name});
    return restore(credential);
  }

  Future<void> revoke(OpaqueCredential credential) async {
    try {
      await _request('sign-out', session: credential.session, body: {});
    } on IdentityException catch (error) {
      if (error.failure != IdentityFailure.unauthorized) rethrow;
    }
  }
}
