/// Returns whether a cached Better Auth bridge credential is safe to use.
///
/// Better Auth's staging bridge issues a fixed-lifetime JWT and does not expose
/// a refresh token. Keep a small safety window so an authenticated request is
/// never started with a token that is about to expire.
bool isBetterAuthSessionUsable({
  required String token,
  required String uid,
  required int expirationTime,
  DateTime? now,
  Duration minimumValidity = const Duration(minutes: 5),
}) {
  if (token.isEmpty || uid.isEmpty || expirationTime <= 0) return false;
  final currentTime = now ?? DateTime.now();
  return expirationTime > currentTime.add(minimumValidity).millisecondsSinceEpoch;
}
