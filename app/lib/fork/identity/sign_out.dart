import 'owner.dart';

/// Revocation must succeed before destructive settings cleanup. The existing
/// credential commit queue fences a newer login for the entire async clear.
Future<bool> completeNativeSignOut({
  required IdentityOwner owner,
  required Future<void> Function() revoke,
  required Future<void> Function() clearLocalState,
}) async {
  await revoke();
  return owner.clearSignedOutState(clearLocalState);
}
