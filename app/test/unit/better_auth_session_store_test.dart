import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/auth/better_auth_session_store.dart';

void main() {
  setUp(() {
    FlutterSecureStorage.setMockInitialValues({});
  });

  test('session bearer is persisted in secure storage and restored at startup', () async {
    final first = BetterAuthSessionStore();
    await first.save('signed-session');

    final restored = BetterAuthSessionStore();
    await restored.load();

    expect(restored.sessionToken, 'signed-session');
  });

  test('clear removes both memory and secure storage copies', () async {
    final store = BetterAuthSessionStore();
    await store.save('signed-session');
    await store.clear();

    final restored = BetterAuthSessionStore();
    await restored.load();
    expect(store.sessionToken, isEmpty);
    expect(restored.sessionToken, isEmpty);
  });
}
