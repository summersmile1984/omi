import 'package:flutter_test/flutter_test.dart';
import 'package:omi/env/environment_profile.dart';
import 'package:omi/services/connectivity_service.dart';

void main() {
  test('constructing a capture dependency does not require Env before connectivity starts', () {
    expect(ConnectivityService.new, returnsNormally);
  });

  test('self-hosted connectivity probes only the selected backend', () {
    final uris = connectivityCheckUris(
      profile: AppEnvironmentProfile.selfHosted,
      apiBaseUrl: 'https://api.example.com/',
    );

    expect(uris, [Uri.parse('https://api.example.com/v1/health')]);
    expect(uris.any((uri) => uri.host == 'api.omi.me' || uri.host == 'one.one.one.one'), isFalse);
  });

  test('managed connectivity retains its independent public reachability probe', () {
    final uris = connectivityCheckUris(
      profile: AppEnvironmentProfile.production,
      apiBaseUrl: 'https://api.omi.me/',
    );

    expect(uris, [Uri.parse('https://one.one.one.one'), Uri.parse('https://api.omi.me/v1/health')]);
  });
}
