#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:?output directory is required}"
bundle_id="${2:?self-hosted bundle id is required}"
callback_scheme="${3:?auth callback scheme is required}"
app_group="${4:-group.${bundle_id}}"

if [[ ! "$bundle_id" =~ ^[A-Za-z0-9-]+([.][A-Za-z0-9-]+)+$ ]]; then
  echo "invalid self-hosted bundle id: ${bundle_id}" >&2
  exit 2
fi
if [[ ! "$callback_scheme" =~ ^[A-Za-z][A-Za-z0-9+.-]*$ ]]; then
  echo "invalid auth callback scheme: ${callback_scheme}" >&2
  exit 2
fi
if [[ ! "$app_group" =~ ^group[.][A-Za-z0-9-]+([.][A-Za-z0-9-]+)+$ ]]; then
  echo "invalid self-hosted app group: ${app_group}" >&2
  exit 2
fi

mkdir -p "$output_dir"
temp_file="$(mktemp "${output_dir}/Custom.xcconfig.tmp.XXXXXX")"
trap 'rm -f "$temp_file"' EXIT

cat >"$temp_file" <<EOF
// Generated self-hosted iOS authority. Do not check in environment-specific values.
APP_BUNDLE_IDENTIFIER=${bundle_id}
APP_GROUP_IDENTIFIER=${app_group}
AUTH_CALLBACK_SCHEME=${callback_scheme}
RUNNER_INFOPLIST_FILE=Runner/Info-SelfHost.plist
RUNNER_CODE_SIGN_ENTITLEMENTS=Runner/RunnerSelfHost.entitlements
FIREBASE_SERVICES_ENABLED=NO
EOF

mv "$temp_file" "${output_dir}/Custom.xcconfig"
trap - EXIT
