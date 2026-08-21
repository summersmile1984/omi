import 'package:flutter/material.dart';

import 'package:firebase_crashlytics/firebase_crashlytics.dart';
import 'package:talker_flutter/talker_flutter.dart';

import 'package:omi/env/environment_profile.dart';
import 'package:omi/env/firebase_services_policy.dart';
import 'package:omi/utils/analytics/intercom.dart';
import 'package:omi/utils/debug_log_manager.dart';
import 'package:omi/utils/l10n_extensions.dart';

class CrashlyticsTalkerObserver extends TalkerObserver {
  CrashlyticsTalkerObserver();

  @override
  void onError(err) {
    FirebaseCrashlytics.instance.recordError(err.error, err.stackTrace, reason: err.message);
  }

  @override
  void onException(err) {
    FirebaseCrashlytics.instance.recordError(err.exception, err.stackTrace, reason: err.message);
  }
}

typedef TalkerObserverFactory = TalkerObserver Function();

class Logger {
  late final Talker talker;

  Logger._() {
    talker = TalkerFlutter.init(
      observer: FirebaseServicesPolicy.enabled ? CrashlyticsTalkerObserver() : null,
    );
  }

  @visibleForTesting
  Logger.forTesting({
    required AppEnvironmentProfile profile,
    required bool firebaseServicesEnabled,
    required TalkerObserverFactory createCrashObserver,
  }) {
    talker = _createTalker(
      profile: profile,
      firebaseServicesEnabled: firebaseServicesEnabled,
      createCrashObserver: createCrashObserver,
    );
  }

  static Talker _createTalker({
    required AppEnvironmentProfile profile,
    required bool firebaseServicesEnabled,
    required TalkerObserverFactory createCrashObserver,
  }) {
    final observer = FirebaseServicesPolicy.allowsFor(
      profile: profile,
      configuredEnabled: firebaseServicesEnabled,
    )
        ? createCrashObserver()
        : null;
    return TalkerFlutter.init(observer: observer);
  }

  static final Logger _instance = Logger._();

  static Logger get instance => _instance;

  static void log(dynamic message) {
    instance.talker.log(message);
  }

  static void error(dynamic message) {
    instance.talker.error(message);
    DebugLogManager.logError(message);
  }

  static void warning(dynamic message) {
    instance.talker.warning(message);
    DebugLogManager.logWarning(message.toString());
  }

  static void info(dynamic message) {
    instance.talker.info(message);
  }

  static void debug(dynamic message) {
    instance.talker.debug(message);
  }

  static void handle(dynamic exception, StackTrace? stackTrace, {String? message}) {
    instance.talker.handle(exception, stackTrace, message ?? 'An error occurred. Please try again later.');
    DebugLogManager.logError(exception, stackTrace, message);
  }
}

class LoggerSnackbar extends StatelessWidget {
  final TalkerError? error;
  final TalkerException? exception;

  const LoggerSnackbar({super.key, this.error, this.exception}) : assert(error != null || exception != null);

  @override
  Widget build(BuildContext context) {
    final data = error ?? exception!;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(color: Colors.red, borderRadius: BorderRadius.circular(10)),
      child: ListTile(
        contentPadding: const EdgeInsets.all(0),
        leading: const Icon(Icons.error_outline, color: Colors.white),
        title: Text(
          data.message ?? context.l10n.somethingWentWrongTryAgain,
          style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold),
        ),
        trailing: IconButton(
          icon: const Icon(Icons.share, color: Colors.white),
          onPressed: () async {
            // TODO: Have a custom form which can be prefilled with the error stack trace instead of opening the Gleap Homepage
            await IntercomManager.instance.displayMessenger();
          },
        ),
      ),
    );
  }
}
