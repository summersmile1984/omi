#!/usr/bin/env bash
# Fork-owned. Upstream's test-app-config.sh in this same directory asserts
# app-config.sh's own default behavior and stays byte-identical to upstream
# (dev/unified-main/00-upstream-touch-policy.md: upstream tests run
# unmodified in upstream mode; fork behavior is asserted here instead).
#
# Covers what app-config.sh's OMI_NAMED_BUNDLE_SLUG_PREFIX /
# OMI_NAMED_BUNDLE_ID_PREFIX overrides add (scripts/brand/generators/desktop.py
# writes these into app-config.brand.sh, which app-config.sh sources).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MACOS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$MACOS_DIR/scripts/app-config.sh"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_eq() {
  local expected="$1" actual="$2" label="$3"
  if [ "$expected" != "$actual" ]; then
    fail "$label: expected '$expected', got '$actual'"
  fi
}

# A brand prefix other than "omi" derives the bundle id from it, not from
# the "com.omi." literal.
OMI_NAMED_BUNDLE_SLUG_PREFIX="acme" OMI_NAMED_BUNDLE_ID_PREFIX="com.acme." \
  derive_omi_app_config "acme-feature-test"
assert_eq "com.acme.acme-feature-test" "$BUNDLE_ID" "BUNDLE_ID under an acme brand prefix"

# The old "omi-" prefix is rejected once the brand prefix is "acme" --
# proves the case pattern actually reads the override, not just accepts
# anything.
if OMI_NAMED_BUNDLE_SLUG_PREFIX="acme" derive_omi_app_config "omi-should-be-rejected" \
     >/tmp/omi-app-config-brand-prefix.out 2>/tmp/omi-app-config-brand-prefix.err; then
  fail "the old omi- prefix unexpectedly succeeded under an acme- brand prefix"
fi

# No override at all -- the common case, and the one every existing
# developer workflow depends on: a fresh checkout that has never run
# apply.py, or a checkout for the omi-upstream brand, must reproduce
# exactly today's behavior via app-config.sh's own ${VAR:-omi} fallback.
unset OMI_NAMED_BUNDLE_SLUG_PREFIX OMI_NAMED_BUNDLE_ID_PREFIX
derive_omi_app_config "omi-subagent-test"
assert_eq "com.omi.omi-subagent-test" "$BUNDLE_ID" "BUNDLE_ID with no brand override"
assert_eq "true" "$IS_NAMED_BUNDLE" "IS_NAMED_BUNDLE with no brand override"

echo "app-config brand-override tests passed"
