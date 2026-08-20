#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app_root="$(cd "${script_dir}/.." && pwd)"

required_env=(
  OMI_API_BASE_URL
  OMI_AUTH_SERVER_URL
  OMI_PRIVACY_URL
  OMI_TERMS_URL
  OMI_SHARE_BASE_URL
  OMI_SELF_HOST_BUNDLE_ID
  OMI_SELF_HOST_APP_GROUP_ID
  OMI_SELF_HOST_AUTH_CALLBACK_SCHEME
)
for name in "${required_env[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "${name} is required for a self-hosted iOS release" >&2
    exit 1
  fi
done
if [[ "${OMI_AUTH_PROVIDER:-better_auth}" != "better_auth" ]]; then
  echo "self-hosted iOS releases require OMI_AUTH_PROVIDER=better_auth" >&2
  exit 1
fi

custom_config="${app_root}/ios/Flutter/Custom.xcconfig"
config_backup="$(mktemp "${TMPDIR:-/tmp}/omi-ios-selfhost-config.XXXXXX")"
analysis_options="${app_root}/analysis_options.yaml"
analysis_backup="$(mktemp "${TMPDIR:-/tmp}/omi-ios-selfhost-analysis.XXXXXX")"
had_config=false
had_analysis=false
if [[ -f "$custom_config" ]]; then
  cp "$custom_config" "$config_backup"
  had_config=true
fi
if [[ -f "$analysis_options" ]]; then
  cp "$analysis_options" "$analysis_backup"
  had_analysis=true
fi
restore_local_files() {
  if [[ "$had_config" == true ]]; then
    cp "$config_backup" "$custom_config"
  else
    rm -f "$custom_config"
  fi
  if [[ "$had_analysis" == true ]]; then
    cp "$analysis_backup" "$analysis_options"
  else
    rm -f "$analysis_options"
  fi
  rm -f "$config_backup"
  rm -f "$analysis_backup"
}
trap restore_local_files EXIT

bash "${script_dir}/generate_ios_self_host_config.sh" \
  "${app_root}/ios/Flutter" \
  "$OMI_SELF_HOST_BUNDLE_ID" \
  "$OMI_SELF_HOST_AUTH_CALLBACK_SCHEME" \
  "$OMI_SELF_HOST_APP_GROUP_ID"

flutter_args=(
  --release
  --flavor prod
  -t lib/main.dart
  --dart-define=OMI_APP_PROFILE=self_hosted
  "--dart-define=OMI_API_BASE_URL=${OMI_API_BASE_URL}"
  --dart-define=OMI_AUTH_PROVIDER=better_auth
  "--dart-define=OMI_AUTH_SERVER_URL=${OMI_AUTH_SERVER_URL}"
  --dart-define=OMI_FIREBASE_SERVICES_ENABLED=false
  "--dart-define=OMI_PRIVACY_URL=${OMI_PRIVACY_URL}"
  "--dart-define=OMI_TERMS_URL=${OMI_TERMS_URL}"
  "--dart-define=OMI_SHARE_BASE_URL=${OMI_SHARE_BASE_URL}"
)
if [[ "${OMI_IOS_NO_CODESIGN:-false}" == "true" ]]; then
  flutter_args+=(--no-codesign)
fi

cd "$app_root"
flutter pub get
dart run build_runner build
flutter build ios "${flutter_args[@]}" "$@"

verify_signature=true
if [[ "${OMI_IOS_NO_CODESIGN:-false}" == "true" ]]; then
  verify_signature=false
fi
bash "${script_dir}/smoke_ios_self_host_artifact.sh" \
  "${app_root}/build/ios/iphoneos/Runner.app" \
  "$OMI_SELF_HOST_BUNDLE_ID" \
  "$OMI_SELF_HOST_AUTH_CALLBACK_SCHEME" \
  "$verify_signature"
