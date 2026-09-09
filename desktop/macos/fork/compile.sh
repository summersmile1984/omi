#!/usr/bin/env bash
# Compile the actual staged consumer without packaging, signing or launching it.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if [ "$(uname -s)" != Darwin ]; then
  echo 'Native app compilation requires the macOS manifest platform.' >&2
  exit 2
fi
cd "$ROOT"
stage="$(mktemp -d "${TMPDIR:-/tmp}/fork-native-compile.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
python3 desktop/macos/fork/ci_build.py --output "$stage/app"
