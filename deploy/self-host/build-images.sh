#!/usr/bin/env bash
# LIFECYCLE: permanent
# Build the exact upstream runtime first, then the fork-only profile/dependency layer.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
ENV_FILE="${SELF_HOST_ENV:?SELF_HOST_ENV is required}"
PY="${PYTHON:-python3}"
BASE_IMAGE="$("$PY" -c 'import sys; from dotenv import dotenv_values; print(dotenv_values(sys.argv[1],interpolate=False)["BACKEND_RUNTIME_IMAGE"])' "$ENV_FILE")"
PLATFORM="$("$PY" -c 'import sys; from dotenv import dotenv_values; print(dotenv_values(sys.argv[1],interpolate=False)["BACKEND_PLATFORM"])' "$ENV_FILE")"
[[ "$BASE_IMAGE" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$ ]] || { echo 'invalid BACKEND_RUNTIME_IMAGE' >&2; exit 1; }
[[ "$PLATFORM" == linux/amd64 ]] || { echo 'the locked backend runtime currently requires linux/amd64' >&2; exit 1; }
docker build --platform "$PLATFORM" --file "$ROOT/backend/Dockerfile" \
  --build-arg PYTHON_BASE_IMAGE=python:3.11.10-slim-bookworm@sha256:840e180ebcc6e5c8efab209c43f5e40fd2af98cb49db5c7103c90539c56bb30e \
  --tag "$BASE_IMAGE" "$ROOT"
bash "$DIR/compose-clean-env.sh" "$ENV_FILE" "$DIR/compose.production.yml" build auth-server backend

# The thin Ollama layer compiles runtime precision/context from that exact profile.
providers="$("$PY" "$DIR/model_services.py" --env-file "$ENV_FILE" --providers)"
case " $providers " in
  *" llm "*) bash "$DIR/compose-clean-env.sh" "$ENV_FILE" "$DIR/compose.production.yml" build llm ;;
esac
