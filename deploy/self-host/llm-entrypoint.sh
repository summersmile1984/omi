#!/bin/sh
# LIFECYCLE: permanent
# Build-generated, validated public settings override inherited daemon options.
set -eu
. /etc/llm-runtime.env
exec /bin/ollama "$@"
