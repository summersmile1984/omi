import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'credential.dart';
import 'deployment.dart';
import 'owner.dart';

/// This namespace never reads the shipped Firebase JWT or Android JWT mirror.
final class SecureOpaqueCredentialStore implements OpaqueCredentialStore {
  SecureOpaqueCredentialStore(NativeDeployment deployment)
      : _storage = FlutterSecureStorage(
          iOptions:
              IOSOptions(accountName: deployment.keychainPrefix, accessibility: KeychainAccessibility.first_unlock),
          aOptions: AndroidOptions(
              encryptedSharedPreferences: true,
              sharedPreferencesName: deployment.keychainPrefix,
              preferencesKeyPrefix: deployment.keychainPrefix),
        );
  final FlutterSecureStorage _storage;
  static const _key = 'better-auth-session-v1';
  @override
  Future<OpaqueCredential?> read() async {
    final value = await _storage.read(key: _key);
    return value == null ? null : OpaqueCredential.fromJson(jsonDecode(value));
  }

  @override
  Future<void> write(OpaqueCredential credential) => _storage.write(key: _key, value: jsonEncode(credential.toJson()));
  @override
  Future<void> clear() => _storage.delete(key: _key);
}
