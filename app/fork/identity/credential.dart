import 'package:omi/services/auth/auth_token_result.dart';

final class OpaqueCredential {
  const OpaqueCredential({required this.session, required this.user});
  final String session;
  final AuthUserSnapshot user;
  factory OpaqueCredential.fromJson(Map<String, dynamic> value) {
    final token = value['session'];
    if (token is! String || token.isEmpty) throw const FormatException('Missing stored session');
    return OpaqueCredential(session: token, user: parseUser(value['user']));
  }
  Map<String, dynamic> toJson() => {
        'session': session,
        'user': {
          'id': user.uid,
          'email': user.email,
          'name': user.displayName,
        }
      };
  static AuthUserSnapshot parseUser(dynamic value) {
    if (value is! Map<String, dynamic> || value['id'] is! String || (value['id'] as String).isEmpty) {
      throw const FormatException('Missing authenticated user');
    }
    return AuthUserSnapshot(
        uid: value['id'] as String, email: value['email'] as String?, displayName: value['name'] as String?);
  }
}

enum IdentityFailure { unauthorized, unavailable, invalidResponse, rejected, storageUnavailable, superseded }

final class IdentityException implements Exception {
  const IdentityException(this.failure);
  final IdentityFailure failure;
  @override
  String toString() => 'IdentityException(${failure.name})';
}
