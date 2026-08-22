import 'package:flutter_test/flutter_test.dart';
import 'package:omi/services/notifications/notification_service.dart';

void main() {
  test('self-hosted notification policy never constructs Firebase Messaging', () {
    final service = NotificationService.createForTesting(firebaseServicesEnabled: false);

    expect(service.usesFirebaseMessaging, isFalse);
    expect(service.deliveryCapability, NotificationDeliveryCapability.localOnly);
  });
}
