#!/usr/bin/env bash

set -euo pipefail

app_profile="${OMI_APP_PROFILE:-mobile_beta}"
android_flavor='prod'
if [[ "${OMI_AUTH_PROVIDER:-}" == "better_auth" && -z "${OMI_APP_PROFILE:-}" ]]; then
  app_profile='self_hosted'
fi
if [[ "${OMI_AUTH_PROVIDER:-}" == "better_auth" && "$app_profile" != 'self_hosted' ]]; then
  echo "OMI_AUTH_PROVIDER=better_auth requires OMI_APP_PROFILE=self_hosted for this release lane" >&2
  exit 1
fi
if [[ "$app_profile" == 'self_hosted' && "${OMI_AUTH_PROVIDER:-}" != "better_auth" ]]; then
  echo "OMI_APP_PROFILE=self_hosted requires OMI_AUTH_PROVIDER=better_auth for this release lane" >&2
  exit 1
fi
if [[ "$app_profile" == 'self_hosted' ]]; then
  android_flavor='selfhost'
fi

flutter_args=(--release --flavor "$android_flavor" -t lib/main.dart "--dart-define=OMI_APP_PROFILE=${app_profile}")

if [[ -n "${OMI_API_BASE_URL:-}" ]]; then
  flutter_args+=("--dart-define=OMI_API_BASE_URL=${OMI_API_BASE_URL}")
fi
if [[ -n "${OMI_AUTH_PROVIDER:-}" ]]; then
  if [[ "${OMI_AUTH_PROVIDER}" == "better_auth" && -z "${OMI_AUTH_SERVER_URL:-}" ]]; then
    echo "OMI_AUTH_SERVER_URL is required when OMI_AUTH_PROVIDER=better_auth" >&2
    exit 1
  fi
  if [[ "${OMI_AUTH_PROVIDER}" == "better_auth" && -z "${OMI_API_BASE_URL:-}" ]]; then
    echo "OMI_API_BASE_URL is required when OMI_AUTH_PROVIDER=better_auth" >&2
    exit 1
  fi
  if [[ "${OMI_AUTH_PROVIDER}" == "better_auth" ]]; then
    for public_origin in OMI_PRIVACY_URL OMI_TERMS_URL OMI_SHARE_BASE_URL OMI_MCP_BASE_URL; do
      if [[ -z "${!public_origin:-}" ]]; then
        echo "${public_origin} is required when OMI_AUTH_PROVIDER=better_auth" >&2
        exit 1
      fi
      flutter_args+=("--dart-define=${public_origin}=${!public_origin}")
    done
  fi
  flutter_args+=("--dart-define=OMI_AUTH_PROVIDER=${OMI_AUTH_PROVIDER}")
  if [[ "${OMI_AUTH_PROVIDER}" == "better_auth" ]]; then
    export OMI_FIREBASE_SERVICES_ENABLED=false
    flutter_args+=("--dart-define=OMI_FIREBASE_SERVICES_ENABLED=false")
  fi
  if [[ -n "${OMI_AUTH_SERVER_URL:-}" ]]; then
    flutter_args+=("--dart-define=OMI_AUTH_SERVER_URL=${OMI_AUTH_SERVER_URL}")
  fi
fi

flutter clean
if [[ "$app_profile" == 'self_hosted' ]]; then
  source scripts/self_host_env_guard.sh
  with_self_host_env_guard "$PWD" bash -c '
    set -e
    flutter pub get --enforce-lockfile
    dart run build_runner build
    bash scripts/check_self_host_generated_env.sh
    flutter build appbundle "$@"
    flutter build apk "$@"
  ' self-host-build "${flutter_args[@]}"
  bash scripts/smoke_android_self_host_artifact.sh build/app/outputs/bundle/selfhostRelease/app-selfhost-release.aab
  bash scripts/smoke_android_self_host_artifact.sh build/app/outputs/flutter-apk/app-selfhost-release.apk
else
  flutter pub get
  dart run build_runner build
  flutter build appbundle "${flutter_args[@]}"
  flutter build apk "${flutter_args[@]}"
fi
