#!/bin/sh
# LIFECYCLE: permanent
# workerd verifies cached Pyodide bundle integrity against its compiled digest.
set -eu
: "${CLOUDFLARE_WORKERD_BINARY:?use scripts/python-worker.mjs}"
: "${CLOUDFLARE_PYODIDE_CACHE_DIR:?use scripts/python-worker.mjs}"
exec "$CLOUDFLARE_WORKERD_BINARY" "$@" \
  --pyodide-bundle-disk-cache-dir="$CLOUDFLARE_PYODIDE_CACHE_DIR" \
  --pyodide-package-disk-cache-dir="$CLOUDFLARE_PYODIDE_CACHE_DIR"
