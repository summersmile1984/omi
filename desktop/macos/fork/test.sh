#!/usr/bin/env bash
# Deterministic native identity lane. Live services belong to the named bundle
# bridge acceptance path documented in README.md, never the CI test runner.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if [ "$(uname -s)" != Darwin ]; then
  echo 'Native identity tests require macOS; select the macos manifest platform.' >&2
  exit 2
fi
cd "$ROOT"
xcrun swift test --package-path desktop/macos/fork
python3 desktop/macos/fork/test_stage.py
