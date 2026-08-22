import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:flutter_timezone/flutter_timezone.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/services/notifications/notification_interface.dart';
import 'package:omi/services/notifications/notification_service_basic.dart';

typedef OperatorPushRegistrationTransport = Future<http.Response?> Function({
  required String url,
  required Map<String, String> headers,
  required String body,
});

typedef OperatorAuthHeaderProvider = Future<String> Function();
typedef OperatorTimeZoneProvider = Future<String> Function();

/// Local notifications plus an explicitly operator-owned token registration
/// seam. This class does not construct Firebase or interpret the token; the
/// configured operator authority owns delivery-provider semantics.
class OperatorNotificationService extends BasicNotificationService {
  OperatorNotificationService({
    required this.registrationBaseUrl,
    OperatorPushRegistrationTransport? transport,
    OperatorAuthHeaderProvider? authHeaderProvider,
    OperatorTimeZoneProvider? timeZoneProvider,
    String Function()? platformProvider,
  })  : _transport = transport ?? _liveTransport,
        _authHeaderProvider = authHeaderProvider ?? _liveAuthHeader,
        _timeZoneProvider = timeZoneProvider ?? _liveTimeZone,
        _platformProvider = platformProvider ?? _defaultPlatform,
        super();

  final String registrationBaseUrl;
  final OperatorPushRegistrationTransport _transport;
  final OperatorAuthHeaderProvider _authHeaderProvider;
  final OperatorTimeZoneProvider _timeZoneProvider;
  final String Function() _platformProvider;

  @override
  NotificationDeliveryCapability get deliveryCapability => NotificationDeliveryCapability.operatorRemote;

  @override
  Future<void> register() => Future<void>.error(
        UnsupportedError('Operator push registration requires an explicit platform token.'),
      );

  /// The released method name is retained for callers that already receive a
  /// platform token. The value is provider-neutral and is never sent to
  /// Firebase-specific code by this service.
  @override
  Future<void> saveFcmToken(String? token) async {
    if (token == null || token.trim().isEmpty) return;
    await registerOperatorToken(token);
  }

  @override
  void saveNotificationToken() {
    throw UnsupportedError('Operator push registration requires an explicit platform token.');
  }

  @override
  Future<void> registerOperatorToken(String token) async {
    final normalizedToken = token.trim();
    if (normalizedToken.isEmpty) {
      throw ArgumentError.value(token, 'token', 'must not be empty');
    }

    final authHeader = await _authHeaderProvider();
    final timeZone = await _timeZoneProvider();
    final platform = _platformProvider();
    final body = jsonEncode({
      // The backend keeps this released field name for wire compatibility;
      // token_type makes the provider-neutral meaning explicit.
      'fcm_token': normalizedToken,
      'token_type': 'opaque_registered_token',
      'platform': platform,
      'time_zone': timeZone,
    });
    final response = await _transport(
      url: '${registrationBaseUrl.replaceFirst(RegExp(r'/+$'), '')}/v1/users/fcm-token',
      headers: {
        'Authorization': authHeader,
        'Content-Type': 'application/json',
        'X-App-Platform': platform,
      },
      body: body,
    );
    if (response == null || response.statusCode < 200 || response.statusCode >= 300) {
      throw StateError('Operator notification registration failed.');
    }
  }

  static Future<http.Response?> _liveTransport({
    required String url,
    required Map<String, String> headers,
    required String body,
  }) {
    return makeApiCall(
      url: url,
      headers: headers,
      method: 'POST',
      body: body,
      signOutOn401: false,
    );
  }

  static Future<String> _liveAuthHeader() => getAuthHeader(expireTerminalSession: false);

  static Future<String> _liveTimeZone() async {
    return FlutterTimezone.getLocalTimezone();
  }

  static String _defaultPlatform() => defaultTargetPlatform.name.toLowerCase();
}

NotificationInterface createNotificationService({
  required String registrationBaseUrl,
  OperatorPushRegistrationTransport? transport,
  OperatorAuthHeaderProvider? authHeaderProvider,
  OperatorTimeZoneProvider? timeZoneProvider,
  String Function()? platformProvider,
}) {
  return OperatorNotificationService(
    registrationBaseUrl: registrationBaseUrl,
    transport: transport,
    authHeaderProvider: authHeaderProvider,
    timeZoneProvider: timeZoneProvider,
    platformProvider: platformProvider,
  );
}
