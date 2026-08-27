import 'dart:async';

import 'package:flutter/material.dart';

import 'package:awesome_notifications/awesome_notifications.dart';
import 'package:flutter_timezone/flutter_timezone.dart';

import 'package:omi/backend/schema/message.dart';
import 'package:omi/services/notifications/notification_interface.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/notification_channel_strings.dart';

/// Basic notification service for platforms without Firebase Messaging support
/// Used by Windows and self-hosted releases; provides local notifications only.
class BasicNotificationService implements NotificationInterface {
  BasicNotificationService();

  @override
  NotificationDeliveryCapability get deliveryCapability => NotificationDeliveryCapability.localOnly;

  @override
  bool get usesFirebaseMessaging => false;

  // Resolved in initialize() after NotificationChannelStrings.loadAppLocale().
  late final NotificationChannel channel;

  final AwesomeNotifications _awesomeNotifications = AwesomeNotifications();

  @override
  Future<void> initialize() async {
    await NotificationChannelStrings.loadAppLocale();
    channel = NotificationChannel(
      channelGroupKey: 'channel_group_key',
      channelKey: 'channel',
      channelName: NotificationChannelStrings.omiChannelName,
      channelDescription: NotificationChannelStrings.omiChannelDescription,
      defaultColor: const Color(0xFF9D50DD),
      ledColor: Colors.white,
    );
    await _initializeAwesomeNotifications();
    Logger.debug('Basic notification service initialized (Firebase Messaging not available on this platform)');
  }

  Future<void> _initializeAwesomeNotifications() async {
    bool initialized = await _awesomeNotifications.initialize(
      // set the icon to null if you want to use the default app icon
      'resource://drawable/icon',
      [
        NotificationChannel(
          channelGroupKey: 'channel_group_key',
          channelKey: channel.channelKey,
          channelName: channel.channelName,
          channelDescription: channel.channelDescription,
          defaultColor: const Color(0xFF9D50DD),
          ledColor: Colors.white,
        ),
      ],
      // Channel groups are only visual and are not required
      channelGroups: [
        NotificationChannelGroup(channelGroupKey: channel.channelKey!, channelGroupName: channel.channelName!),
      ],
      debug: false,
    );

    Logger.debug('initializeNotifications: $initialized');
  }

  @override
  Future<void> showNotification({
    required int id,
    required String title,
    required String body,
    Map<String, String?>? payload,
    bool wakeUpScreen = false,
    NotificationSchedule? schedule,
    NotificationLayout layout = NotificationLayout.Default,
  }) async {
    final allowed = await _awesomeNotifications.isNotificationAllowed();
    if (!allowed) {
      return;
    }
    try {
      await _awesomeNotifications.createNotification(
        content: NotificationContent(
          id: id,
          channelKey: channel.channelKey!,
          actionType: ActionType.Default,
          title: title,
          body: body,
          payload: payload,
          notificationLayout: layout,
        ),
      );
    } catch (e) {
      Logger.debug('Failed to create notification (channel may be disabled): $e');
    }
  }

  @override
  Future<bool> requestNotificationPermissions() async {
    bool isAllowed = await _awesomeNotifications.isNotificationAllowed();
    if (!isAllowed) {
      isAllowed = await _awesomeNotifications.requestPermissionToSendNotifications();
    }
    return isAllowed;
  }

  @override
  Future<void> register() async {
    throw UnsupportedError('Remote notification registration is unavailable for the local-only provider.');
  }

  @override
  Future<void> registerOperatorToken(String token) async {
    throw UnsupportedError('Operator push registration is unavailable for the local-only provider.');
  }

  @override
  Future<String> getTimeZone() async {
    final timezone = await FlutterTimezone.getLocalTimezone();
    return timezone.identifier;
  }

  @override
  Future<void> saveFcmToken(String? token) async {
    throw UnsupportedError('FCM token storage is unavailable for the local-only provider.');
  }

  @override
  void saveNotificationToken() {
    throw UnsupportedError('Notification token storage is unavailable for the local-only provider.');
  }

  @override
  Future<bool> hasNotificationPermissions() async {
    return await _awesomeNotifications.isNotificationAllowed();
  }

  @override
  Future<void> createNotification({
    String title = '',
    String body = '',
    int notificationId = 1,
    Map<String, String?>? payload,
  }) async {
    var allowed = await _awesomeNotifications.isNotificationAllowed();
    Logger.debug('createNotification: $allowed');
    if (!allowed) return;
    Logger.debug('createNotification ~ Creating notification: $title');
    showNotification(id: notificationId, title: title, body: body, wakeUpScreen: true, payload: payload);
  }

  @override
  void clearNotification(int id) => _awesomeNotifications.cancel(id);

  @override
  Future<void> listenForMessages() async {
    // Firebase Cloud Messaging not supported on this platform
    // Local notifications still work, but no remote messaging
    Logger.debug('Firebase message listening not available on this platform');
  }

  final _serverMessageStreamController = StreamController<ServerMessage>.broadcast();

  @override
  Stream<ServerMessage> get listenForServerMessages => _serverMessageStreamController.stream;
}

/// Factory function to create the basic notification service
NotificationInterface createNotificationService() => BasicNotificationService();
