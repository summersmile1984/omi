import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import 'package:firebase_crashlytics/firebase_crashlytics.dart';
import 'package:firebase_core/firebase_core.dart';

import 'package:omi/env/firebase_services_policy.dart';

class CrashlyticsManager {
  static final CrashlyticsManager _instance = CrashlyticsManager._internal();
  static CrashlyticsManager get instance => _instance;

  CrashlyticsManager._internal();

  factory CrashlyticsManager() {
    return _instance;
  }

  static bool get _isAvailable => FirebaseServicesPolicy.enabled && Firebase.apps.isNotEmpty;

  static Future<void> init() async {
    if (!_isAvailable) return;
    // Disable Crashlytics collection in debug mode
    if (kDebugMode) {
      await FirebaseCrashlytics.instance.setCrashlyticsCollectionEnabled(false);
    } else {
      await FirebaseCrashlytics.instance.setCrashlyticsCollectionEnabled(true);
    }
  }

  void identifyUser(String email, String name, String userId) {
    if (!_isAvailable) return;
    FirebaseCrashlytics.instance.setUserIdentifier(userId);
    if (email.isNotEmpty) {
      FirebaseCrashlytics.instance.setCustomKey('user_email', email);
    }
    if (name.isNotEmpty) {
      FirebaseCrashlytics.instance.setCustomKey('user_name', name);
    }
  }

  void logInfo(String message) {
    if (!_isAvailable) return;
    FirebaseCrashlytics.instance.log(message);
  }

  void logError(String message) {
    if (!_isAvailable) return;
    FirebaseCrashlytics.instance.log('ERROR: $message');
  }

  void logWarn(String message) {
    if (!_isAvailable) return;
    FirebaseCrashlytics.instance.log('WARN: $message');
  }

  void logDebug(String message) {
    if (!_isAvailable) return;
    FirebaseCrashlytics.instance.log('DEBUG: $message');
  }

  void logVerbose(String message) {
    if (!_isAvailable) return;
    FirebaseCrashlytics.instance.log('VERBOSE: $message');
  }

  void setUserAttribute(String key, String value) {
    if (!_isAvailable) return;
    FirebaseCrashlytics.instance.setCustomKey(key, value);
  }

  void setEnabled(bool isEnabled) {
    if (!_isAvailable) return;
    FirebaseCrashlytics.instance.setCrashlyticsCollectionEnabled(isEnabled);
  }

  Future<void> reportCrash(
    Object exception,
    StackTrace stackTrace, {
    Map<String, String>? userAttributes,
  }) async {
    if (!_isAvailable) return;
    if (userAttributes != null) {
      for (final entry in userAttributes.entries) {
        await FirebaseCrashlytics.instance.setCustomKey(entry.key, entry.value);
      }
    }
    await FirebaseCrashlytics.instance.recordError(exception, stackTrace);
  }

  NavigatorObserver? getNavigatorObserver() {
    return null;
  }

  bool get isSupported => FirebaseServicesPolicy.enabled;
}
