/// Runs Firebase background work only for a deployment that opted into the
/// managed Firebase runtime.
///
/// The native messaging plugin can invoke a registered Dart entrypoint after
/// an app variant changes. Keeping this guard at the entrypoint boundary means
/// a self-hosted build never initializes Firebase merely because a stale
/// background delivery reaches the process.
Future<void> runFirebaseBackgroundWorkIfEnabled({
  required bool enabled,
  required Future<void> Function() work,
}) async {
  if (!enabled) return;
  await work();
}
