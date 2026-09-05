#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT/app"
flutter --version --machine | python3 -c 'import json,sys; v=json.load(sys.stdin); assert v["frameworkVersion"] == "3.44.5", "Use pinned Flutter 3.44.5"'
export DART
DART=$(command -v dart)
test -f .dart_tool/package_config.json || { echo 'Run flutter pub get --enforce-lockfile in app first'; exit 1; }
flutter test --no-pub test/fork/native_identity_test.dart
node --test fork/tests/assets.test.mjs
python3 fork/test_stage.py
TEMP_STAGE=$(mktemp -d "${TMPDIR:-/tmp}/omi-fork-flutter.XXXXXX")
trap 'rm -rf "$TEMP_STAGE"' EXIT
python3 fork/fixture.py "$TEMP_STAGE" > "$TEMP_STAGE/brand.json"
for target in self_hosted cloudflare; do
  python3 fork/prepare.py --manifest "$TEMP_STAGE/brand.json" --target "$target" --output "$TEMP_STAGE/$target" --dart "$DART"
  mkdir -p "$TEMP_STAGE/$target/app/test/fork"
  cp test/fork/*.dart "$TEMP_STAGE/$target/app/test/fork/"
  cp fork/tests/gateway_test.dart.txt "$TEMP_STAGE/$target/app/test/fork/gateway_test.dart"
  cp fork/tests/assets_test.dart.txt "$TEMP_STAGE/$target/app/test/fork/brand_assets_test.dart"
  (
    cd "$TEMP_STAGE/$target/app"
    flutter pub get --offline --enforce-lockfile
    dart run build_runner build
    flutter test --no-pub --dart-define-from-file=../defines.json test/fork/
    # Compiles the real staged main and every reachable Dart consumer. It is
    # not an APK install, native Gradle compile or emulator/UI qualification.
    flutter build bundle --debug --no-pub --dart-define-from-file=../defines.json
  )
done
