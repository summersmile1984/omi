import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:omi/fork/identity/client.dart';
import 'package:omi/fork/identity/credential.dart';
import 'package:omi/fork/identity/deployment.dart';
import 'package:omi/fork/identity/owner.dart';
import 'package:omi/fork/identity/sign_out.dart';
import 'package:omi/services/auth/auth_token_result.dart';

NativeDeployment profile([String target = 'self_hosted']) => NativeDeployment.decode(jsonEncode({
      'package_id': 'invalid.example.fixture.$target',
      'product_name': 'Identity Proof',
      'privacy': 'https://example.invalid/privacy',
      'terms': 'https://example.invalid/terms',
      'profile': {
        'target': target,
        'stage': 'local',
        'name': '$target.local',
        'identity_provider': 'better_auth',
        for (final key in ['api', 'auth', 'web', 'mcp', 'share', 'objects']) '${key}_base_url': 'http://127.0.0.1:33067'
      },
    }));
const userA = AuthUserSnapshot(uid: 'owner-a', email: 'a@example.invalid', displayName: 'A');
const storedA = OpaqueCredential(session: 'opaque-a', user: userA);
final now = DateTime.utc(2026, 9, 4), seconds = now.millisecondsSinceEpoch ~/ 1000;
String jwt({String uid = 'owner-a', int? iat, int? exp, String sid = 'session-a'}) =>
    'header.${base64Url.encode(utf8.encode(jsonEncode({
              'uid': uid,
              'sub': uid,
              'sid': sid,
              'iat': iat ?? seconds,
              'exp': exp ?? seconds + 300
            }))).replaceAll('=', '')}.signature';
http.Response login([String uid = 'owner-a']) => http.Response(
    jsonEncode({
      'user': {'id': uid}
    }),
    200,
    headers: {'set-auth-token': 'opaque-$uid'});
Matcher fails(IdentityFailure failure) =>
    throwsA(isA<IdentityException>().having((e) => e.failure, 'failure', failure));

class MemoryStore implements OpaqueCredentialStore {
  OpaqueCredential? value;
  bool failWrite = false;
  Completer<void>? pause;
  @override
  Future<OpaqueCredential?> read() async => value;
  @override
  Future<void> write(OpaqueCredential credential) async {
    if (failWrite) throw StateError('storage fixture');
    value = credential;
    await pause?.future;
  }

  @override
  Future<void> clear() async {
    value = null;
  }
}

void main() {
  test('settings clear shares the credential commit queue and preserves a newer owner', () async {
    final store = MemoryStore();
    final owner = IdentityOwner(
        store: store, client: IdentityClient(deployment: profile(), transport: (_) async => login('owner-b')));
    var clearCount = 0;
    await expectLater(
        completeNativeSignOut(
            owner: owner,
            revoke: () async => throw const IdentityException(IdentityFailure.unavailable),
            clearLocalState: () async {
              clearCount++;
            }),
        fails(IdentityFailure.unavailable));
    expect(clearCount, 0);
    final clear = Completer<void>(), entered = Completer<void>();
    var preferences = 'old-owner';
    owner.changes.listen((user) {
      preferences = user?.uid ?? '';
    });
    final pending = completeNativeSignOut(
        owner: owner,
        revoke: owner.signOut,
        clearLocalState: () async {
          clearCount++;
          entered.complete();
          await clear.future;
          preferences = '';
        });
    await entered.future;
    final newerLogin = owner.authenticate(email: 'b@example.invalid', password: 'synthetic');
    await Future<void>.delayed(Duration.zero);
    expect(owner.currentUser, isNull);
    clear.complete();
    expect(await pending, isFalse);
    await newerLogin;
    expect(owner.currentUser?.uid, 'owner-b');
    expect(store.value?.user.uid, 'owner-b');
    expect(preferences, 'owner-b');
    expect(clearCount, 1);
    expect(
        await owner.clearSignedOutState(() async {
          preferences = '';
        }),
        isFalse);
    expect(preferences, 'owner-b');
  });

  for (final target in ['self_hosted', 'cloudflare']) {
    test('$target login, restored owner, refresh and remote-first logout use opaque Bearer', () async {
      final seen = <http.Request>[];
      final store = MemoryStore();
      final client = IdentityClient(
          deployment: profile(target),
          now: () => now,
          transport: (request) async {
            seen.add(request);
            expect(request.followRedirects, isFalse);
            switch (request.url.path) {
              case '/api/auth/sign-in/email':
                return login();
              case '/api/auth/get-session':
                return http.Response(
                    jsonEncode({
                      'user': {'id': 'owner-a'}
                    }),
                    200);
              case '/api/auth/token':
                return http.Response(jsonEncode({'token': jwt()}), 200);
              case '/api/auth/sign-out':
                return http.Response('{}', 200);
              default:
                throw StateError('unexpected request');
            }
          });
      var owner = IdentityOwner(client: client, store: store);
      await owner.authenticate(email: 'a@example.invalid', password: 'synthetic password');
      expect(owner.currentUser?.uid, 'owner-a');
      expect(store.value?.session, 'opaque-owner-a');
      owner = IdentityOwner(client: client, store: store);
      await owner.restore();
      expect(owner.currentUser?.uid, 'owner-a');
      final token = await owner.forceRefresh();
      expect(token?.token, jwt());
      expect(token?.expirationTime, now.add(const Duration(minutes: 5)));
      expect(store.value?.session, 'opaque-owner-a');
      await owner.signOut();
      expect(owner.currentUser, isNull);
      expect(store.value, isNull);
      for (final request in seen.skip(1)) {
        expect(request.headers['Authorization'], 'Bearer opaque-owner-a');
        expect(request.url.query, isEmpty);
      }
    });
  }
  test('unconfigured topology fails and secure namespace separates targets and authority', () {
    expect(() => NativeDeployment.decode('{}'), throwsFormatException);
    expect(profile().keychainPrefix, isNot(profile('cloudflare').keychainPrefix));
    final wrong = jsonDecode(jsonEncode({
      'profile': {'target': 'omi_cloud'}
    }));
    expect(() => NativeDeployment.decode(jsonEncode(wrong)), throwsFormatException);
  });
  test('bad password is a rejected sign-in, not an expired previously authenticated owner', () async {
    final client = IdentityClient(deployment: profile(), transport: (_) async => http.Response('{}', 401));
    await expectLater(client.authenticate(email: 'a', password: 'b'), fails(IdentityFailure.rejected));
  });
  test('legacy JWT-only stored shape cannot become an opaque session', () {
    expect(() => OpaqueCredential.fromJson({'token': jwt(), 'uid': 'owner-a'}), throwsFormatException);
  });
  test('missing session is cleared, but 503 and malformed restore preserve durable retry', () async {
    for (final response in [http.Response('null', 200), http.Response('{}', 503), http.Response('{bad', 200)]) {
      final store = MemoryStore()..value = storedA;
      final owner =
          IdentityOwner(client: IdentityClient(deployment: profile(), transport: (_) async => response), store: store);
      if (response.body == 'null') {
        await owner.restore();
        expect(store.value, isNull);
      } else {
        await expectLater(owner.restore(),
            fails(response.statusCode == 503 ? IdentityFailure.unavailable : IdentityFailure.invalidResponse));
        expect(store.value?.session, storedA.session);
      }
      expect(owner.currentUser, isNull);
    }
  });
  test('sign-out outage preserves committed owner; revoked 401 completes logout', () async {
    var status = 503;
    final owner = IdentityOwner(
        client: IdentityClient(
            deployment: profile(),
            transport: (request) async =>
                request.url.path.endsWith('sign-in/email') ? login() : http.Response('{}', status)),
        store: MemoryStore());
    await owner.authenticate(email: 'a', password: 'b');
    await expectLater(owner.signOut(), fails(IdentityFailure.unavailable));
    expect(owner.currentUser?.uid, 'owner-a');
    status = 401;
    await owner.signOut();
    expect(owner.currentUser, isNull);
  });
  test('a later login owns both durable and visible identity', () async {
    final a = Completer<http.Response>(), store = MemoryStore();
    final owner = IdentityOwner(
        client: IdentityClient(
            deployment: profile(),
            transport: (request) async => jsonDecode(request.body)['email'] == 'a' ? await a.future : login('owner-b')),
        store: store);
    final pending = owner.authenticate(email: 'a', password: 'password');
    final rejected = expectLater(pending, fails(IdentityFailure.superseded));
    await owner.authenticate(email: 'b', password: 'password');
    a.complete(login());
    await rejected;
    expect(owner.currentUser?.uid, 'owner-b');
    expect(store.value?.user.uid, 'owner-b');
  });
  test('logout fences a pending login even before a session exists', () async {
    final pendingResponse = Completer<http.Response>(), store = MemoryStore();
    final owner = IdentityOwner(
        client: IdentityClient(deployment: profile(), transport: (_) => pendingResponse.future), store: store);
    final pending = owner.authenticate(email: 'a', password: 'password');
    final rejected = expectLater(pending, fails(IdentityFailure.superseded));
    await owner.signOut();
    pendingResponse.complete(login());
    await rejected;
    expect(owner.currentUser, isNull);
    expect(store.value, isNull);
  });
  test('logout during asynchronous durable write rolls back before publishing', () async {
    final store = MemoryStore()..pause = Completer<void>();
    final owner =
        IdentityOwner(client: IdentityClient(deployment: profile(), transport: (_) async => login()), store: store);
    final changes = <AuthUserSnapshot?>[];
    owner.changes.listen(changes.add);
    final pending = owner.authenticate(email: 'a', password: 'password');
    final rejected = expectLater(pending, fails(IdentityFailure.superseded));
    await Future<void>.delayed(Duration.zero);
    final logout = owner.signOut();
    store.pause!.complete();
    await rejected;
    await logout;
    expect(changes.whereType<AuthUserSnapshot>(), isEmpty);
    expect(store.value, isNull);
    expect(owner.currentUser, isNull);
  });
  test('durable write failure never publishes a login', () async {
    final store = MemoryStore()..failWrite = true;
    final owner =
        IdentityOwner(client: IdentityClient(deployment: profile(), transport: (_) async => login()), store: store);
    await expectLater(owner.authenticate(email: 'a', password: 'b'), fails(IdentityFailure.storageUnavailable));
    expect(owner.currentUser, isNull);
    expect(store.value, isNull);
  });
  test('issued-at cache admission permits sixty seconds of device clock skew', () async {
    for (final offset in [30, 60]) {
      final token = jwt(iat: seconds + offset, exp: seconds + offset + 300);
      final client = IdentityClient(
          deployment: profile(),
          now: () => now,
          transport: (_) async => http.Response(jsonEncode({'token': token}), 200));
      expect((await client.refresh(storedA)).token, token);
    }
  });
  test('expired, wrong owner, missing sid, oversized and extreme lifetime JWTs fail cache admission', () async {
    for (final token in [
      jwt(exp: seconds),
      jwt(iat: seconds + 61, exp: seconds + 361),
      jwt(uid: 'owner-b'),
      jwt(sid: ''),
      jwt(exp: seconds + 3601),
      jwt(iat: -9223372036854775808)
    ]) {
      final client = IdentityClient(
          deployment: profile(),
          now: () => now,
          transport: (_) async => http.Response(jsonEncode({'token': token}), 200));
      await expectLater(client.refresh(storedA), fails(IdentityFailure.invalidResponse));
    }
  });
}
