import 'package:omi/flavors.dart';

import 'environment_profile.dart';

abstract class Env {
  static const productionApiBaseUrl = 'https://api.omi.me/';
  static const productionAgentProxyWsUrl = 'wss://agent.omi.me/v1/agent/ws';
  static const privacyPolicyUrl = String.fromEnvironment('OMI_PRIVACY_URL');
  static const termsOfServiceUrl = String.fromEnvironment('OMI_TERMS_URL');
  static const shareBaseUrl = String.fromEnvironment('OMI_SHARE_BASE_URL');
  static const firebaseServicesEnabled = bool.fromEnvironment('OMI_FIREBASE_SERVICES_ENABLED', defaultValue: true);
  static const _apiBaseUrlFromDefine = String.fromEnvironment('OMI_API_BASE_URL');
  static const firebaseAuthEmulatorHost = String.fromEnvironment(
    'OMI_FIREBASE_AUTH_EMULATOR_HOST',
    defaultValue: '127.0.0.1',
  );
  static const _firebaseAuthEmulatorPort = String.fromEnvironment(
    'OMI_FIREBASE_AUTH_EMULATOR_PORT',
    defaultValue: '9099',
  );
  static late final EnvFields _instance;
  static String? _apiBaseUrlOverride;
  static String? _agentProxyWsUrlOverride;
  static bool isTestFlight = false;

  static AppEnvironmentProfile get profile =>
      AppEnvironmentProfile.forFlavor(productionFlavor: F.env == Environment.prod);

  static void init(EnvFields instance) {
    _instance = instance;
  }

  static void overrideApiBaseUrl(String url) {
    _apiBaseUrlOverride = url;
  }

  static void clearApiBaseUrlOverrideForTesting() {
    _apiBaseUrlOverride = null;
  }

  static void overrideAgentProxyWsUrl(String url) {
    _agentProxyWsUrlOverride = url;
  }

  static String? get openAIAPIKey => _instance.openAIAPIKey;

  static String? get posthogApiKey => _instance.posthogApiKey;

  // static String? get apiBaseUrl => 'https://omi-backend.ngrok.app/';
  static String? get apiBaseUrl {
    if (_apiBaseUrlOverride != null) return _apiBaseUrlOverride;
    if (_apiBaseUrlFromDefine.isNotEmpty) return _apiBaseUrlFromDefine;
    final configuredApiBaseUrl = _instance.apiBaseUrl;
    if (configuredApiBaseUrl != null && configuredApiBaseUrl.isNotEmpty) {
      return configuredApiBaseUrl;
    }
    return profile.defaultApiBaseUrl;
  }

  static int get firebaseAuthEmulatorPort => int.tryParse(_firebaseAuthEmulatorPort) ?? 9099;

  static String get authCallbackScheme => profile.authCallbackScheme;

  static String get authRedirectUri => '$authCallbackScheme://auth/callback';

  /// OAuth remains on the production identity plane even when mobile Beta
  /// uses the development serving API for product traffic.
  static String get authApiBaseUrl => authApiBaseUrlForProfile(profile, servingApiBaseUrl: apiBaseUrl);

  static String authApiBaseUrlForProfile(AppEnvironmentProfile configuredProfile, {String? servingApiBaseUrl}) {
    if (configuredProfile == AppEnvironmentProfile.mobileBeta) {
      return productionApiBaseUrl;
    }
    return servingApiBaseUrl ?? configuredProfile.defaultApiBaseUrl;
  }

  static String get betterAuthServerUrl => const String.fromEnvironment('OMI_AUTH_SERVER_URL');

  static void validateProfilePairing() {
    final productionFlavor = F.env == Environment.prod;
    if (!productionFlavor && profile != AppEnvironmentProfile.localDev) {
      throw StateError('Profile ${profile.name} must be built with the prod flavor.');
    }
    if (productionFlavor && profile == AppEnvironmentProfile.localDev) {
      throw StateError('The prod flavor cannot use the local_dev profile.');
    }
  }

  /// Self-hosted builds must provide their legal/share origins explicitly.
  /// Managed builds retain the established Omi URLs and do not need these
  /// compile-time overrides.
  static void validateClientPublicOrigins({
    AppEnvironmentProfile? configuredProfile,
    String? configuredPrivacyUrl,
    String? configuredTermsUrl,
    String? configuredShareUrl,
  }) {
    final effectiveProfile = configuredProfile ?? profile;
    if (effectiveProfile != AppEnvironmentProfile.selfHosted) return;
    for (final origin in {
      'OMI_PRIVACY_URL': configuredPrivacyUrl ?? privacyPolicyUrl,
      'OMI_TERMS_URL': configuredTermsUrl ?? termsOfServiceUrl,
      'OMI_SHARE_BASE_URL': configuredShareUrl ?? shareBaseUrl,
    }.entries) {
      final uri = Uri.tryParse(origin.value.trim());
      if (uri == null ||
          uri.scheme != 'https' ||
          uri.host.isEmpty ||
          uri.userInfo.isNotEmpty ||
          uri.hasQuery ||
          uri.hasFragment ||
          _isOmiOperatedHost(uri.host)) {
        throw StateError('Profile self_hosted requires ${origin.key} to use an explicit non-Omi HTTPS URL.');
      }
    }
  }

  static String resolveShareBaseUrl({AppEnvironmentProfile? configuredProfile, String? configuredShareUrl}) {
    final effectiveProfile = configuredProfile ?? profile;
    var value = (configuredShareUrl ?? shareBaseUrl).trim();
    if (effectiveProfile == AppEnvironmentProfile.selfHosted) {
      final uri = Uri.tryParse(value);
      if (uri == null ||
          uri.scheme != 'https' ||
          uri.host.isEmpty ||
          uri.userInfo.isNotEmpty ||
          uri.hasQuery ||
          uri.hasFragment ||
          _isOmiOperatedHost(uri.host)) {
        throw StateError('Profile self_hosted requires OMI_SHARE_BASE_URL to use an explicit non-Omi HTTPS URL.');
      }
      return value.replaceFirst(RegExp(r'/+$'), '');
    }
    if (value.isEmpty) return 'https://h.omi.me';
    if (!value.contains('://')) value = 'https://$value';
    final uri = Uri.tryParse(value);
    if (uri == null ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment ||
        (uri.scheme != 'http' && uri.scheme != 'https')) {
      return 'https://h.omi.me';
    }
    final origin = uri.hasPort ? '${uri.scheme}://${uri.host}:${uri.port}' : '${uri.scheme}://${uri.host}';
    final path = uri.path.replaceFirst(RegExp(r'/+$'), '');
    return path.isEmpty ? origin : '$origin$path';
  }

  /// Canonical operator origin used by API/auth and other client-owned
  /// authorities. Credentials, paths, queries and fragments are rejected so
  /// a server response cannot smuggle a second authority into a URL join.
  static String canonicalSelfHostedOrigin(String raw, {required String key, bool releaseBuild = true}) {
    final value = raw.trim();
    final uri = Uri.tryParse(value);
    if (uri == null ||
        uri.host.isEmpty ||
        (uri.scheme != 'http' && uri.scheme != 'https') ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment ||
        (uri.path.isNotEmpty && uri.path != '/')) {
      throw StateError('Profile self_hosted requires $key to be an absolute origin without credentials or path.');
    }
    if (releaseBuild && uri.scheme != 'https') {
      throw StateError('Profile self_hosted requires $key to use HTTPS in release builds.');
    }
    if (_isOmiOperatedHost(uri.host)) {
      throw StateError('Profile self_hosted cannot use an Omi-operated origin for $key.');
    }
    final defaultPort = (uri.scheme == 'https' && uri.port == 443) || (uri.scheme == 'http' && uri.port == 80);
    final canonical = Uri(
      scheme: uri.scheme.toLowerCase(),
      host: uri.host.toLowerCase().replaceFirst(RegExp(r'\.+$'), ''),
      port: uri.hasPort && !defaultPort ? uri.port : null,
    );
    return canonical.origin;
  }

  static void validateFirebaseProject({required String projectId, AppEnvironmentProfile? configuredProfile}) {
    final effectiveProfile = configuredProfile ?? profile;
    if (projectId != effectiveProfile.firebaseProjectId) {
      throw StateError(
        'Mobile profile ${effectiveProfile.name} requires Firebase project ${effectiveProfile.firebaseProjectId}, '
        'but the app was initialized with $projectId.',
      );
    }
  }

  /// Production-family packages have one pinned backend authority. This runs
  /// during startup so a misconfigured signing group fails before networking.
  static void validateStartupRouting({
    required bool productionFamily,
    String? configuredApiBaseUrl,
    AppEnvironmentProfile? configuredProfile,
    bool releaseBuild = true,
  }) {
    final effectiveProfile = configuredProfile ?? (productionFamily ? AppEnvironmentProfile.production : profile);
    final normalized = (configuredApiBaseUrl ?? apiBaseUrl ?? '').trim().replaceFirst(RegExp(r'/+$'), '');
    final expected = effectiveProfile.defaultApiBaseUrl.replaceFirst(RegExp(r'/+$'), '');

    if (effectiveProfile == AppEnvironmentProfile.localDev) {
      if (!_isLocalDevelopmentApi(normalized)) {
        throw StateError(
          'Profile local_dev requires a loopback or private-network API endpoint; '
          'use mobile_beta for https://api.omiapi.com/.',
        );
      }
      return;
    }

    if (effectiveProfile == AppEnvironmentProfile.selfHosted) {
      canonicalSelfHostedOrigin(
        configuredApiBaseUrl ?? apiBaseUrl ?? '',
        key: 'OMI_API_BASE_URL',
        releaseBuild: releaseBuild,
      );
      return;
    }

    if (normalized != expected) {
      throw StateError('Profile ${effectiveProfile.name} requires API_BASE_URL=${effectiveProfile.defaultApiBaseUrl}');
    }

    if (effectiveProfile == AppEnvironmentProfile.production &&
        _agentProxyWsUrlFor(normalized) != productionAgentProxyWsUrl) {
      throw StateError('Production packages require the production agent WebSocket endpoint.');
    }
  }

  static void requireProductionRouting() => validateStartupRouting(productionFamily: true);

  /// WebSocket URL for the agent proxy service.
  /// Derives from apiBaseUrl: api.omi.me → agent.omi.me, api.omiapi.com → agent.omiapi.com.
  /// Can be overridden via Env.overrideAgentProxyWsUrl() for local testing.
  static String get agentProxyWsUrl {
    if (_agentProxyWsUrlOverride != null) return _agentProxyWsUrlOverride!;
    return _agentProxyWsUrlFor(apiBaseUrl ?? productionApiBaseUrl);
  }

  static String _agentProxyWsUrlFor(String base) {
    final host = Uri.parse(base).host.replaceFirst('api.', 'agent.');
    return 'wss://$host/v1/agent/ws';
  }

  static bool _isLocalDevelopmentApi(String base) {
    final uri = Uri.tryParse(base);
    if (uri == null || uri.host.isEmpty || (uri.scheme != 'http' && uri.scheme != 'https')) {
      return false;
    }
    final host = uri.host.toLowerCase();
    if (host == 'localhost' || host == 'host.docker.internal' || host == '::1') {
      return true;
    }
    final octets = host.split('.').map(int.tryParse).toList();
    if (octets.length != 4 || octets.any((octet) => octet == null || octet < 0 || octet > 255)) {
      return false;
    }
    final first = octets[0]!;
    final second = octets[1]!;
    return first == 10 ||
        (first == 172 && second >= 16 && second <= 31) ||
        (first == 192 && second == 168) ||
        (first == 127);
  }

  static bool _isOmiOperatedHost(String host) {
    final normalized = host.toLowerCase().replaceFirst(RegExp(r'\.+$'), '');
    return normalized == 'omi.me' ||
        normalized.endsWith('.omi.me') ||
        normalized == 'omiapi.com' ||
        normalized.endsWith('.omiapi.com');
  }

  static String? get googleMapsApiKey => profile.managedClientValue(_instance.googleMapsApiKey);

  static String? get intercomAppId => profile.managedClientValue(_instance.intercomAppId);

  static String? get intercomIOSApiKey => profile.managedClientValue(_instance.intercomIOSApiKey);

  static String? get intercomAndroidApiKey => profile.managedClientValue(_instance.intercomAndroidApiKey);

  static String? get googleClientId => profile.managedClientValue(_instance.googleClientId);

  static String? get googleClientSecret => profile.managedClientValue(_instance.googleClientSecret);

  static bool get useWebAuth => profile.managedClientValue(_instance.useWebAuth) ?? false;

  static bool get useAuthCustomToken => profile.managedClientValue(_instance.useAuthCustomToken) ?? false;
}

abstract class EnvFields {
  String? get openAIAPIKey;

  String? get posthogApiKey;

  String? get apiBaseUrl;

  String? get googleMapsApiKey;

  String? get intercomAppId;

  String? get intercomIOSApiKey;

  String? get intercomAndroidApiKey;

  String? get googleClientId;

  String? get googleClientSecret;

  bool? get useWebAuth;

  bool? get useAuthCustomToken;
}
