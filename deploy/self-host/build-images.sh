#!/usr/bin/env bash
# LIFECYCLE: permanent
# Build the exact upstream runtime first, then the fork-only profile/dependency layer.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
ENV_FILE="${SELF_HOST_ENV:?SELF_HOST_ENV is required}"
PY="${PYTHON:-python3}"
BASE_IMAGE="$("$PY" -c 'import sys; values=dict(line.strip().split("=",1) for line in open(sys.argv[1]) if line.strip() and not line.lstrip().startswith("#") and "=" in line); print(values["BACKEND_RUNTIME_IMAGE"])' "$ENV_FILE")"
PLATFORM="$("$PY" -c 'import sys; values=dict(line.strip().split("=",1) for line in open(sys.argv[1]) if line.strip() and not line.lstrip().startswith("#") and "=" in line); print(values["BACKEND_PLATFORM"])' "$ENV_FILE")"
[[ "$BASE_IMAGE" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$ ]] || { echo 'invalid BACKEND_RUNTIME_IMAGE' >&2; exit 1; }
[[ "$PLATFORM" == linux/amd64 ]] || { echo 'the locked backend runtime currently requires linux/amd64' >&2; exit 1; }
docker build --platform "$PLATFORM" --file "$ROOT/backend/Dockerfile" \
  --build-arg PYTHON_BASE_IMAGE=python:3.11.10-slim-bookworm@sha256:840e180ebcc6e5c8efab209c43f5e40fd2af98cb49db5c7103c90539c56bb30e \
  --tag "$BASE_IMAGE" "$ROOT"
bash "$DIR/compose-clean-env.sh" "$ENV_FILE" "$DIR/compose.production.yml" build auth-server backend
