# Flutter upstream compatibility verification

Date: 2026-09-05

## Scope

- Upstream base: `upstream/main` at `520cc70ac06d63af818ba5170a27cea0db1cfefe` (`v0.12.291`).
- Local merge: `552cb91330597d83ebf31221b06e4162dde3b8d1`.
- Follow-up stage repair: `194369a74c`.
- Fork-owned rank regression: `8851d5b57a`.

The upstream update deleted `app/lib/services/notifications/notification_service_basic.dart` while the white-label target deliberately replaces Firebase Cloud Messaging with that basic implementation. The stage now materializes a fork-owned copy at `app/fork/overlays/notification_service_basic.dart.txt`; the removed upstream path is no longer treated as a source-owned input. It also refreshes the source-owner digest for upstream's changed auth service.

## Checks run

```text
PATH="/tmp/memweft-flutter-3.44.5/bin:$PATH" bash app/fork/test.sh
=> exit 0
```

The command used the repository-pinned Flutter 3.44.5 / Dart 3.12.2. It passed 14 native-identity tests, 2 raster tests, and 4 stage-ownership tests. For each `self_hosted` and `cloudflare` staged application it generated dependencies, ran 20 fork tests, and compiled the reachable application entry through `flutter build bundle --debug`.

The new fork-owned test exercises `groupSearchResultsPreservingRank` with two local-time conversations in the same calendar day. It verifies both the day-bucket invariant and the server supplied order, and runs in both staged targets.

## Known upstream test limitation

```text
PATH="/tmp/memweft-flutter-3.44.5/bin:$PATH" \
  flutter test --no-pub test/unit/search_rank_grouping_test.dart
=> exit 1 in Asia/Shanghai
```

The upstream fixture constructs an August 12 UTC time, then adds 2 and 8 hours. In the local Asia/Shanghai timezone those values land on August 12 and August 13, so the test's `grouped.values.single` assertion fails with `Bad state: Too many elements`. The upstream file remains untouched under the fork upstream-touch policy. This means a clean full upstream Flutter suite is not claimed from this timezone; the fork-owned local-day regression above supplies target coverage without changing upstream bytes. An upstream test-fixture correction is still needed before treating the complete upstream suite as green here.

## Limits

This is local stage and debug-bundle evidence. It is not an Android/iOS signed build, an emulator install, device push/OAuth qualification, or a distribution release. No branch was pushed and no remote service was changed.
