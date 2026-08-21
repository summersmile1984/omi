#!/usr/bin/env bash
set -euo pipefail

generated="${1:-lib/env/prod_env.g.dart}"
[[ -f "$generated" ]] || { echo "generated Envied source is missing: $generated" >&2; exit 1; }

for field in posthogApiKey googleMapsApiKey intercomAppId intercomIOSApiKey intercomAndroidApiKey googleClientId googleClientSecret; do
  if ! grep -Eq "static final String[?] ${field} = null;" "$generated"; then
    echo "self-host codegen embedded managed client value: ${field}" >&2
    exit 1
  fi
done

echo 'self-host generated Envied source contains no managed client credentials'
