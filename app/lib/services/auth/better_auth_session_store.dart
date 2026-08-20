import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Keychain/Keystore-backed storage for Better Auth's long-lived bearer session.
///
/// The short-lived backend JWT remains in the existing request cache; only the
/// credential capable of minting new JWTs is held in platform secure storage.
final class BetterAuthSessionStore {
  BetterAuthSessionStore({FlutterSecureStorage? storage}) : _storage = storage ?? const FlutterSecureStorage();

  static final BetterAuthSessionStore instance = BetterAuthSessionStore();
  static const sessionTokenKey = 'better_auth_session_token';

  final FlutterSecureStorage _storage;
  String _sessionToken = '';

  String get sessionToken => _sessionToken;

  Future<void> load() async {
    _sessionToken = await _storage.read(key: sessionTokenKey) ?? '';
  }

  Future<void> save(String token) async {
    if (token.isEmpty) throw ArgumentError.value(token, 'token', 'must not be empty');
    await _storage.write(key: sessionTokenKey, value: token);
    _sessionToken = token;
  }

  Future<void> clear() async {
    _sessionToken = '';
    await _storage.delete(key: sessionTokenKey);
  }
}
