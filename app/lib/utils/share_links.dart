// Public share-link base URL for self-hosting (#4339).
//
// Matches backend `OMI_SHARE_BASE_URL` / desktop share helpers.
// Override at build time with `--dart-define=OMI_SHARE_BASE_URL=https://share.example.com`.

import 'package:omi/env/env.dart';
import 'package:omi/env/environment_profile.dart';

const defaultShareBaseUrl = 'https://h.omi.me';

/// Return the configured share origin (no trailing slash).
///
/// [raw] is for tests; production callers omit it so the dart-define / default apply.
String shareBaseUrl([String? raw]) => shareBaseUrlForProfile(raw, Env.profile);

String shareBaseUrlForProfile(String? raw, AppEnvironmentProfile configuredProfile) => Env.resolveShareBaseUrl(
      configuredProfile: configuredProfile,
      configuredShareUrl: raw,
    );

/// Join [shareBaseUrl] with a path (leading slash optional).
String buildShareUrl(String path, {String? raw}) {
  final normalized = path.startsWith('/') ? path : '/$path';
  return '${shareBaseUrl(raw)}$normalized';
}

String conversationShareUrl(String conversationId, {String? raw}) =>
    buildShareUrl('/conversations/$conversationId', raw: raw);

String appShareUrl(String appId, {String? raw}) => buildShareUrl('/apps/$appId', raw: raw);

String recapShareUrl(String summaryId, {String? raw}) => buildShareUrl('/recaps/$summaryId', raw: raw);
