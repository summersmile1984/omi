import 'package:flutter_test/flutter_test.dart';
import 'package:omi/backend/schema/app.dart';
import 'package:omi/env/env.dart';
import 'package:omi/env/environment_profile.dart';

Map<String, dynamic> _appJson({Object? reviews = _missingReviews, String image = 'https://example.com/icon.png'}) {
  final json = <String, dynamic>{
    'id': 'app-1',
    'name': 'Review parser',
    'author': 'Omi',
    'description': 'Parses reviews',
    'image': image,
    'category': 'productivity',
    'capabilities': ['chat'],
    'rating_count': 0,
    'enabled': true,
    'approved': true,
    'private': false,
  };

  if (!identical(reviews, _missingReviews)) {
    json['reviews'] = reviews;
  }

  return json;
}

const _missingReviews = Object();

void main() {
  group('App review parsing', () {
    test('defaults missing reviews to an empty list', () {
      final app = App.fromJson(_appJson());

      expect(app.reviews, isEmpty);
    });

    test('defaults null reviews to an empty list', () {
      final app = App.fromJson(_appJson(reviews: null));

      expect(app.reviews, isEmpty);
    });

    test('keeps empty reviews as an empty list', () {
      final app = App.fromJson(_appJson(reviews: []));

      expect(app.reviews, isEmpty);
    });

    test('parses present reviews', () {
      final app = App.fromJson(
        _appJson(
          reviews: [
            {
              'uid': 'review-1',
              'rated_at': '2026-07-01T12:00:00.000Z',
              'score': 5.0,
              'review': 'Helpful',
              'username': 'Reviewer',
              'response': 'Thanks',
              'responded_at': '2026-07-02T12:00:00.000Z',
            },
          ],
        ),
      );

      expect(app.reviews, hasLength(1));
      expect(app.reviews.single.uid, 'review-1');
      expect(app.reviews.single.score, 5.0);
      expect(app.reviews.single.review, 'Helpful');
      expect(app.reviews.single.username, 'Reviewer');
      expect(app.reviews.single.response, 'Thanks');
      expect(app.reviews.single.respondedAt, isNotNull);
    });
  });

  group('App image authority', () {
    test('self-hosted relative images use the configured operator API', () {
      expect(
        Env.resolveAppImageUrl(
          image: '/v1/apps/app-1/icon.png',
          configuredProfile: AppEnvironmentProfile.selfHosted,
          configuredApiBaseUrl: 'https://operator.example.test/',
        ),
        'https://operator.example.test/v1/apps/app-1/icon.png',
      );
    });

    test('self-hosted rejects explicit managed image origins', () {
      expect(
        () => Env.resolveAppImageUrl(
          image: 'https://api.omi.me/v1/apps/app-1/icon.png',
          configuredProfile: AppEnvironmentProfile.selfHosted,
          configuredApiBaseUrl: 'https://operator.example.test/',
        ),
        throwsStateError,
      );
      expect(
        () => Env.resolveAppImageUrl(
          image: 'http://operator.example.test/icon.png',
          configuredProfile: AppEnvironmentProfile.selfHosted,
          configuredApiBaseUrl: 'https://operator.example.test/',
        ),
        throwsStateError,
      );
    });
  });
}
