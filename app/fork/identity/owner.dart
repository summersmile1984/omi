import 'dart:async';

import 'package:omi/services/auth/auth_token_result.dart';
import 'client.dart';
import 'credential.dart';

abstract interface class OpaqueCredentialStore {
  Future<OpaqueCredential?> read();
  Future<void> write(OpaqueCredential credential);
  Future<void> clear();
}

/// Owns the long-lived credential. Request adapters only receive derived JWTs.
/// A later login/logout intent supersedes all earlier network completions.
final class IdentityOwner implements AuthTokenGateway {
  IdentityOwner({required IdentityClient client, required OpaqueCredentialStore store})
      : _client = client,
        _store = store;
  final IdentityClient _client;
  final OpaqueCredentialStore _store;
  final _changes = StreamController<AuthUserSnapshot?>.broadcast(sync: true);
  OpaqueCredential? _credential;
  int _epoch = 0;
  Future<void> _mutationTail = Future<void>.value();
  @override
  AuthUserSnapshot? get currentUser => _credential?.user;
  Stream<AuthUserSnapshot?> get changes => _changes.stream;

  void _check(int epoch) {
    if (epoch != _epoch) throw const IdentityException(IdentityFailure.superseded);
  }

  Future<void> _serialize(Future<void> Function() action) {
    final pending = _mutationTail.then((_) => action());
    _mutationTail = pending.then<void>((_) {}, onError: (Object _, StackTrace __) {});
    return pending;
  }

  Future<void> _persist(OpaqueCredential? credential) async {
    try {
      if (credential == null) {
        await _store.clear();
      } else {
        await _store.write(credential);
      }
    } catch (_) {
      throw const IdentityException(IdentityFailure.storageUnavailable);
    }
  }

  Future<void> _commit(int epoch, OpaqueCredential? credential) => _serialize(() async {
        _check(epoch);
        await _persist(credential);
        if (epoch != _epoch) {
          // Storage APIs are asynchronous. Never leave an un-published credential
          // on disk when another user intent arrived during the durable write.
          await _persist(_credential);
          _check(epoch);
        }
        _credential = credential;
        _changes.add(currentUser);
      });

  /// Share the credential commit queue with destructive settings cleanup.
  /// A login may fetch concurrently, but cannot publish or write new user
  /// preferences until the previous user's asynchronous clear has completed.
  Future<bool> clearSignedOutState(Future<void> Function() clear) async {
    final epoch = _epoch;
    var completed = false;
    await _serialize(() async {
      if (_credential != null) return;
      try {
        await clear();
      } catch (_) {
        throw const IdentityException(IdentityFailure.storageUnavailable);
      }
      completed = epoch == _epoch;
    });
    return completed;
  }

  Future<void> authenticate({required String email, required String password, String? name}) async {
    final epoch = ++_epoch;
    final credential = await _client.authenticate(email: email, password: password, name: name);
    _check(epoch);
    await _commit(epoch, credential);
  }

  Future<void> restore() async {
    final epoch = ++_epoch;
    final OpaqueCredential? stored;
    try {
      stored = await _store.read();
    } catch (_) {
      throw const IdentityException(IdentityFailure.storageUnavailable);
    }
    _check(epoch);
    if (stored == null) return;
    try {
      final credential = await _client.restore(stored);
      _check(epoch);
      await _commit(epoch, credential);
    } on IdentityException catch (error) {
      _check(epoch);
      if (error.failure == IdentityFailure.unauthorized) {
        await _commit(epoch, null);
      } else {
        rethrow;
      }
    }
  }

  @override
  Future<RefreshedAuthToken?> forceRefresh() async {
    final epoch = _epoch, credential = _credential;
    if (credential == null) return null;
    final token = await _client.refresh(credential);
    _check(epoch);
    return token;
  }

  @override
  Future<void> signOut() async {
    final epoch = ++_epoch, credential = _credential;
    if (credential != null) await _client.revoke(credential);
    _check(epoch);
    await _commit(epoch, null);
  }

  /// Called only after the existing request owner has classified the session
  /// terminal. A remote outage during ordinary sign-out never uses this path.
  Future<void> invalidate() => _commit(++_epoch, null);

  Future<void> updateName(String name) async {
    final epoch = _epoch, credential = _credential;
    if (credential == null) throw const IdentityException(IdentityFailure.unauthorized);
    final updated = await _client.updateName(credential, name);
    _check(epoch);
    await _commit(epoch, updated);
  }
}
