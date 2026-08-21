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
  OMI_MCP_BASE_URL
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

reserved_self_host_define() {
  case "$1" in
    OMI_APP_PROFILE|OMI_API_BASE_URL|OMI_AUTH_PROVIDER|OMI_AUTH_SERVER_URL|OMI_FIREBASE_SERVICES_ENABLED|OMI_PRIVACY_URL|OMI_TERMS_URL|OMI_SHARE_BASE_URL|OMI_MCP_BASE_URL) return 0 ;;
    *) return 1 ;;
  esac
}
extra_args=("$@")
for ((index = 0; index < ${#extra_args[@]}; index++)); do
  pair=''
  case "${extra_args[$index]}" in
    --dart-define-from-file|--dart-define-from-file=*|--flavor|--flavor=*|-t|--target|--target=*|-t?*|--debug|--profile|--release)
      echo "self-hosted iOS release arguments cannot override build authority with ${extra_args[$index]}" >&2
      exit 2
      ;;
    --dart-define=*) pair="${extra_args[$index]#--dart-define=}" ;;
    --dart-define)
      ((index += 1))
      if ((index >= ${#extra_args[@]})); then
        echo "--dart-define requires a value" >&2
        exit 2
      fi
      pair="${extra_args[$index]}"
      ;;
  esac
  if [[ -n "$pair" ]] && reserved_self_host_define "${pair%%=*}"; then
    echo "self-hosted iOS release arguments cannot override ${pair%%=*}" >&2
    exit 2
  fi
done

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
  "--dart-define=OMI_MCP_BASE_URL=${OMI_MCP_BASE_URL}"
)
if [[ "${OMI_IOS_NO_CODESIGN:-false}" == "true" ]]; then
  flutter_args+=(--no-codesign)
fi

cd "$app_root"
source "${script_dir}/self_host_env_guard.sh"
with_self_host_env_guard "$app_root" bash -c '
  set -e
  flutter pub get --enforce-lockfile
  dart run build_runner build
  bash scripts/check_self_host_generated_env.sh
  flutter build ios "$@"
' self-host-ios "${flutter_args[@]}" "$@"

verify_signature=true
if [[ "${OMI_IOS_NO_CODESIGN:-false}" == "true" ]]; then
  verify_signature=false
fi
bash "${script_dir}/smoke_ios_self_host_artifact.sh" \
  "${app_root}/build/ios/iphoneos/Runner.app" \
  "$OMI_SELF_HOST_BUNDLE_ID" \
  "$OMI_SELF_HOST_AUTH_CALLBACK_SCHEME" \
  "$verify_signature"
