import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/notifications/notification_service_operator.dart';
import 'package:omi/services/notifications/notification_service.dart';

void main() {
  test('self-hosted notification policy never constructs Firebase Messaging', () {
    final service = NotificationService.createForTesting(firebaseServicesEnabled: false);

    expect(service.usesFirebaseMessaging, isFalse);
    expect(service.deliveryCapability, NotificationDeliveryCapability.localOnly);
  });

  test('operator notification registration sends an opaque token to the configured authority', () async {
    String? requestUrl;
    Map<String, String>? requestHeaders;
    String? requestBody;

    final service = OperatorNotificationService(
      registrationBaseUrl: 'https://push.example.com/',
      authHeaderProvider: () async => 'Bearer better-auth-token',
      timeZoneProvider: () async => 'Asia/Shanghai',
      platformProvider: () => 'android',
      transport: ({required url, required headers, required body}) async {
        requestUrl = url;
        requestHeaders = headers;
        requestBody = body;
        return http.Response('{}', 204);
      },
    );

    expect(service.deliveryCapability, NotificationDeliveryCapability.operatorRemote);
    expect(service.usesFirebaseMessaging, isFalse);
    expect(
      await service.registerOperatorTokenIfSupported('  opaque-apns-token  '),
      RemoteNotificationActionResult.completed,
    );
    expect(requestUrl, 'https://push.example.com/v1/users/fcm-token');
    expect(requestHeaders, containsPair('Authorization', 'Bearer better-auth-token'));
    expect(requestHeaders, containsPair('X-App-Platform', 'android'));
    expect(jsonDecode(requestBody!) as Map<String, dynamic>, {
      'fcm_token': 'opaque-apns-token',
      'token_type': 'opaque_registered_token',
      'platform': 'android',
      'time_zone': 'Asia/Shanghai',
    });
  });

  test('local-only provider reports operator registration as unavailable', () async {
    final service = NotificationService.createForTesting(firebaseServicesEnabled: false);

    expect(
      await service.registerOperatorTokenIfSupported('opaque-token'),
      RemoteNotificationActionResult.unsupported,
    );
  });
}
