// Fork-owned. Asserts F.title actually derives from the generated
// kBrandDisplayName constant (scripts/brand/generators/mobile.py writes
// app/lib/flavors.brand.dart, which flavors.dart imports) rather than a
// hardcoded literal. kBrandDisplayName is a compile-time const, so this
// can't exercise a *different* brand's value in-process -- that variance is
// covered on the generator side, in scripts/brand/test_brand_tooling.py.
// This test guards the wiring: if flavors.dart ever reverts to a literal,
// it silently stops tracking whatever brand.omi-upstream/manifest.yaml (or
// another brand's manifest) actually says.
import 'package:flutter_test/flutter_test.dart';
import 'package:omi/flavors.brand.dart';
import 'package:omi/flavors.dart';

void main() {
  tearDown(() => F.env = Environment.fromFlavor());

  test('prod title is exactly the brand display name', () {
    F.env = Environment.prod;
    expect(F.title, kBrandDisplayName);
  });

  test('dev title is the brand display name plus " Dev"', () {
    F.env = Environment.dev;
    expect(F.title, '$kBrandDisplayName Dev');
  });
}
