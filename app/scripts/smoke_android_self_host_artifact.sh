#!/usr/bin/env bash
set -euo pipefail

artifact="${1:?usage: smoke_android_self_host_artifact.sh <apk-or-aab>}"
if [[ ! -f "$artifact" ]]; then
  echo "self-host artifact not found: $artifact" >&2
  exit 1
fi

entries="$(unzip -Z1 "$artifact")"
if grep -Eiq '(^|/)(google-services\.json|GoogleService-Info\.plist|google_app_id\.xml)$' <<<"$entries"; then
  echo "self-host Android artifact contains managed Firebase configuration" >&2
  exit 1
fi

# DEX/resources may legitimately retain guarded Firebase adapter code. Populated
# project credentials and official managed origins are not configuration-neutral.
scan_dir="$(mktemp -d "${TMPDIR:-/tmp}/omi-selfhost-android.XXXXXX")"
cleanup() {
  rm -rf "$scan_dir"
}
trap cleanup EXIT
# Android archives can contain distinct case-sensitive entries such as res/1C.png
# and res/1c.png. Extracting those onto a case-insensitive host would either
# prompt or overwrite one payload, so stream every decompressed entry instead.
scan_file="$scan_dir/decompressed-strings.txt"
unzip -p "$artifact" | strings > "$scan_file"
if grep -Ei 'AIza[0-9A-Za-z_-]{30,}|phc_[0-9A-Za-z_-]{12,}|[0-9]+-[0-9A-Za-z_-]+\.apps\.googleusercontent\.com|[a-z0-9-]+\.firebaseapp\.com|[a-z0-9-]+\.firebaseio\.com' "$scan_file" >/dev/null; then
  echo "self-host Android artifact contains populated managed-client credentials/origins" >&2
  exit 1
fi

echo "self-host Android artifact contains no packaged Firebase configuration or credentials"
