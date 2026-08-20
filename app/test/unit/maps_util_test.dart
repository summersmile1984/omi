import 'package:flutter_test/flutter_test.dart';
import 'package:omi/pages/conversation_detail/maps_util.dart';

void main() {
  test('static map is typed unavailable without an explicitly configured key', () {
    expect(MapsUtil.getMapImageUrl(1, 2, apiKey: ''), isNull);
  });

  test('static map uses the explicitly configured provider key', () {
    final url = MapsUtil.getMapImageUrl(1, 2, apiKey: 'operator-key');

    expect(url, isNotNull);
    expect(Uri.parse(url!).host, 'maps.googleapis.com');
    expect(Uri.parse(url).queryParameters['key'], 'operator-key');
  });
}
