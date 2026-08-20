#!/usr/bin/env bash
set -euo pipefail

artifact="${1:?Runner.app path is required}"
expected_bundle_id="${2:?expected bundle id is required}"
expected_callback_scheme="${3:?expected callback scheme is required}"
verify_signature="${4:-true}"

info_plist="${artifact}/Info.plist"
[[ -f "$info_plist" ]] || { echo "self-hosted iOS artifact is missing Info.plist: ${artifact}" >&2; exit 1; }

actual_bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$info_plist")"
actual_callback_scheme="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleURLTypes:0:CFBundleURLSchemes:0' "$info_plist")"
[[ "$actual_bundle_id" == "$expected_bundle_id" ]] || {
  echo "self-hosted iOS bundle mismatch: ${actual_bundle_id}" >&2
  exit 1
}
[[ "$actual_callback_scheme" == "$expected_callback_scheme" ]] || {
  echo "self-hosted iOS callback mismatch: ${actual_callback_scheme}" >&2
  exit 1
}

if find "$artifact" -iname 'GoogleService-Info.plist' -print -quit | grep -q .; then
  echo 'self-hosted iOS artifact packaged GoogleService-Info.plist' >&2
  exit 1
fi
[[ ! -e "${artifact}/Config" ]] || { echo 'self-hosted iOS artifact packaged native provider config' >&2; exit 1; }
if find "$artifact" -maxdepth 1 -name '*.xcconfig' -print -quit | grep -q .; then
  echo 'self-hosted iOS artifact packaged Xcode authority files' >&2
  exit 1
fi
if plutil -convert json -o - "$info_plist" | grep -Eiq 'h[.]omi[.]me|googleusercontent[.]com'; then
  echo 'self-hosted iOS Info.plist retained an official callback identity' >&2
  exit 1
fi

if find "$artifact" -type f -print0 \
  | xargs -0 strings \
  | grep -Eiq 'AIza[0-9A-Za-z_-]{30,}|phc_[0-9A-Za-z_-]{12,}|[0-9]+-[0-9A-Za-z_-]+\.apps\.googleusercontent\.com|[a-z0-9-]+\.firebaseapp\.com|[a-z0-9-]+\.firebaseio\.com'; then
  echo 'self-hosted iOS artifact retained populated managed-client credentials/origins' >&2
  exit 1
fi

if [[ "$verify_signature" == true ]]; then
  codesign --verify --deep --strict "$artifact"
fi

echo "self-hosted iOS artifact smoke OK: ${actual_bundle_id}"
