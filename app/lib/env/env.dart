import 'package:flutter/foundation.dart';

import 'package:omi/flavors.dart';

import 'environment_profile.dart';

abstract class Env {
  static const productionApiBaseUrl = 'https://api.omi.me/';
  static const _apiBaseUrlFromDefine = String.fromEnvironment(
    'OMI_API_BASE_URL',
  );
  static const privacyPolicyUrl = String.fromEnvironment('OMI_PRIVACY_URL');
  static const termsOfServiceUrl = String.fromEnvironment('OMI_TERMS_URL');
  static const shareBaseUrl = String.fromEnvironment('OMI_SHARE_BASE_URL');
  static const _mcpBaseUrlFromDefine = String.fromEnvironment('OMI_MCP_BASE_URL');
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

  static String? get posthogApiKey => profile.managedClientValue(_instance.posthogApiKey);

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

  static String resolveMcpBaseUrl({
    AppEnvironmentProfile? configuredProfile,
    String? configuredMcpBaseUrl,
    String? configuredApiBaseUrl,
  }) {
    final effectiveProfile = configuredProfile ?? profile;
    var value = (configuredMcpBaseUrl ?? _mcpBaseUrlFromDefine).trim();
    if (value.isEmpty && effectiveProfile != AppEnvironmentProfile.selfHosted) {
      value = (configuredApiBaseUrl ?? apiBaseUrl ?? '').trim();
    }
    if (effectiveProfile == AppEnvironmentProfile.selfHosted) {
      return '${canonicalSelfHostedOrigin(value, key: 'OMI_MCP_BASE_URL')}/';
    }
    final uri = Uri.tryParse(value);
    if (uri == null || uri.host.isEmpty || uri.userInfo.isNotEmpty || uri.hasQuery || uri.hasFragment) {
      throw StateError('OMI_MCP_BASE_URL must be an absolute origin.');
    }
    if (uri.scheme != 'http' && uri.scheme != 'https') {
      throw StateError('OMI_MCP_BASE_URL must use HTTP or HTTPS.');
    }
    final origin = uri.hasPort ? '${uri.scheme}://${uri.host}:${uri.port}' : '${uri.scheme}://${uri.host}';
    final path = uri.path.replaceFirst(RegExp(r'/+$'), '');
    return path.isEmpty || path == '/' ? '$origin/' : '$origin$path/';
  }

  static String get mcpSseUrl => '${resolveMcpBaseUrl()}v1/mcp/sse';

  /// OAuth remains on the production identity plane even when mobile Beta
  /// uses the development serving API for product traffic.
  static String get authApiBaseUrl => authApiBaseUrlForProfile(profile, servingApiBaseUrl: apiBaseUrl);

  static String authApiBaseUrlForProfile(AppEnvironmentProfile configuredProfile, {String? servingApiBaseUrl}) {
    if (configuredProfile == AppEnvironmentProfile.mobileBeta) {
      return productionApiBaseUrl;
    }
    return servingApiBaseUrl ?? configuredProfile.defaultApiBaseUrl;
  }

  static void validateProfilePairing() {
    final productionFlavor = F.env == Environment.prod;
    if (!productionFlavor && profile != AppEnvironmentProfile.localDev) {
      throw StateError('Profile ${profile.name} must be built with the prod flavor.');
    }
    if (productionFlavor && profile == AppEnvironmentProfile.localDev) {
      throw StateError('The prod flavor cannot use the local_dev profile.');
    }
  }

  static void validateClientPublicOrigins({
    AppEnvironmentProfile? configuredProfile,
    String? configuredPrivacyUrl,
    String? configuredTermsUrl,
    String? configuredShareUrl,
    String? configuredMcpBaseUrl,
  }) {
    final effectiveProfile = configuredProfile ?? profile;
    if (effectiveProfile != AppEnvironmentProfile.selfHosted) return;
    for (final origin in {
      'OMI_PRIVACY_URL': configuredPrivacyUrl ?? privacyPolicyUrl,
      'OMI_TERMS_URL': configuredTermsUrl ?? termsOfServiceUrl,
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
    canonicalSelfHostedOrigin(configuredShareUrl ?? shareBaseUrl, key: 'OMI_SHARE_BASE_URL');
    canonicalSelfHostedOrigin(configuredMcpBaseUrl ?? _mcpBaseUrlFromDefine, key: 'OMI_MCP_BASE_URL');
  }

  static String resolveShareBaseUrl({
    AppEnvironmentProfile? configuredProfile,
    String? configuredShareUrl,
  }) {
    final effectiveProfile = configuredProfile ?? profile;
    var value = (configuredShareUrl ?? shareBaseUrl).trim();
    if (value.isEmpty && effectiveProfile != AppEnvironmentProfile.selfHosted) {
      value = 'https://h.omi.me';
    }
    if (value.isNotEmpty && !value.contains('://')) {
      value = 'https://$value';
    }
    if (effectiveProfile == AppEnvironmentProfile.selfHosted) {
      return canonicalSelfHostedOrigin(value, key: 'OMI_SHARE_BASE_URL');
    }
    final uri = Uri.tryParse(value);
    final valid = !RegExp(r'\s').hasMatch(value) &&
        uri != null &&
        uri.host.isNotEmpty &&
        RegExp(r'^[A-Za-z0-9.-]+$').hasMatch(uri.host) &&
        uri.userInfo.isEmpty &&
        !uri.hasQuery &&
        !uri.hasFragment &&
        (uri.scheme == 'http' || uri.scheme == 'https');
    if (!valid) {
      return 'https://h.omi.me';
    }
    final origin = uri.hasPort ? '${uri.scheme}://${uri.host}:${uri.port}' : '${uri.scheme}://${uri.host}';
    final path = uri.path.replaceFirst(RegExp(r'/+$'), '');
    return path.isEmpty || path == '/' ? origin : '$origin$path';
  }

  /// Resolves an app-marketplace image without reintroducing a managed host.
  ///
  /// Relative image paths are served by the authority that supplied the app
  /// in a self-hosted build. The old GitHub fallback is retained only for the
  /// managed profiles, where the marketplace is owned by Omi. Absolute URLs
  /// remain supported for operator-owned integrations, but an explicit Omi
  /// origin is rejected in self-hosted mode rather than silently fetched.
  static String resolveAppImageUrl({
    required String image,
    AppEnvironmentProfile? configuredProfile,
    String? configuredApiBaseUrl,
  }) {
    final effectiveProfile = configuredProfile ?? profile;
    final raw = image.trim();
    if (raw.isEmpty) throw StateError('App image URL must not be empty.');

    final parsed = Uri.tryParse(raw);
    if (parsed != null && parsed.hasScheme) {
      if (parsed.scheme != 'http' && parsed.scheme != 'https') {
        throw StateError('App image URL must use HTTP or HTTPS.');
      }
      if (effectiveProfile == AppEnvironmentProfile.selfHosted && _isOmiOperatedHost(parsed.host)) {
        throw StateError('Self-hosted app images cannot use an Omi-operated origin.');
      }
      return raw;
    }

    if (effectiveProfile == AppEnvironmentProfile.selfHosted) {
      final base = canonicalSelfHostedOrigin(
        configuredApiBaseUrl ?? apiBaseUrl ?? '',
        key: 'OMI_API_BASE_URL',
      );
      final path = raw.startsWith('/') ? raw.substring(1) : raw;
      return '$base/$path';
    }

    return 'https://raw.githubusercontent.com/BasedHardware/Omi/main${raw.startsWith('/') ? raw : '/$raw'}';
  }

  static String requireConfiguredApiBaseUrl([String? configuredApiBaseUrl]) {
    final value = (configuredApiBaseUrl ?? apiBaseUrl ?? '').trim();
    final uri = Uri.tryParse(value);
    if (uri == null || !uri.hasScheme || uri.host.isEmpty) {
      throw StateError('A configured absolute OMI_API_BASE_URL is required.');
    }
    return value;
  }

  /// Canonical signed authority for self-hosted API/auth/MCP/share traffic.
  /// Product/legal links are endpoints and intentionally use their own validator.
  static String canonicalSelfHostedOrigin(
    String raw, {
    required String key,
    bool releaseBuild = true,
  }) {
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

  static void validateFirebaseProject({
    required String projectId,
    AppEnvironmentProfile? configuredProfile,
  }) {
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
    bool releaseBuild = kReleaseMode,
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

    if (effectiveProfile == AppEnvironmentProfile.localProd) {
      if (releaseBuild) {
        throw StateError('Profile local_prod is only available in debug builds.');
      }
      final uri = Uri.tryParse(normalized);
      if (uri == null || uri.host.isEmpty || (uri.scheme != 'http' && uri.scheme != 'https')) {
        throw StateError('Profile local_prod requires a valid http(s) API endpoint.');
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
  }

  static void requireProductionRouting() => validateStartupRouting(productionFamily: true);

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
        // 100.64.0.0/10 — RFC 6598 shared address space, the range Tailscale
        // assigns. Included because a physical device has no other route to a
        // developer's local harness: the harness binds loopback only by design,
        // so the device cannot use 127.x, and a plain LAN address does not reach
        // it either. Bounded to the real /10 — 100.63.x and 100.128.x are public.
        (first == 100 && second >= 64 && second <= 127) ||
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
