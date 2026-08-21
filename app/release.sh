#!/usr/bin/env bash

set -euo pipefail

app_profile="${OMI_APP_PROFILE:-mobile_beta}"
android_flavor='prod'

# Better Auth without an explicit managed profile is the self-hosted release
# path. Never silently turn that request into a Firebase/prod artifact.
if [[ "${OMI_AUTH_PROVIDER:-}" == "better_auth" && -z "${OMI_APP_PROFILE:-}" ]]; then
  app_profile='self_hosted'
fi
if [[ "${OMI_AUTH_PROVIDER:-}" == "better_auth" && "$app_profile" != 'self_hosted' ]]; then
  echo "OMI_AUTH_PROVIDER=better_auth requires OMI_APP_PROFILE=self_hosted; refusing managed Firebase profile." >&2
  exit 1
fi
if [[ "$app_profile" == 'self_hosted' ]]; then
  android_flavor='selfhost'
  if [[ "${OMI_AUTH_PROVIDER:-}" != "better_auth" ]]; then
    echo "self_hosted release requires OMI_AUTH_PROVIDER=better_auth; refusing Firebase fallback." >&2
    exit 1
  fi
  if [[ "${OMI_FIREBASE_SERVICES_ENABLED:-false}" != "false" ]]; then
    echo "self_hosted release requires OMI_FIREBASE_SERVICES_ENABLED=false." >&2
    exit 1
  fi
  for required_origin in OMI_API_BASE_URL OMI_AUTH_SERVER_URL OMI_PRIVACY_URL OMI_TERMS_URL OMI_SHARE_BASE_URL OMI_MCP_BASE_URL; do
    required_value="${!required_origin:-}"
    if [[ -z "$required_value" || "$required_value" =~ [[:space:]] ]]; then
      echo "$required_origin is required for a self_hosted release." >&2
      exit 1
    fi
    if [[ "$required_value" != https://* ]]; then
      echo "$required_origin must use HTTPS for a self_hosted release." >&2
      exit 1
    fi
  done
  export OMI_FIREBASE_SERVICES_ENABLED=false
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
flutter pub get --enforce-lockfile
dart run build_runner build
flutter build appbundle "${flutter_args[@]}"
flutter build apk "${flutter_args[@]}"
