#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
node -e 'if (process.versions.node.split(".")[0] !== "22") throw new Error("Electron fork checks require Node 22")'
case "$(pnpm --version)" in 10.*) ;; *) echo 'Use pinned pnpm major 10' >&2; exit 1;; esac
pnpm exec vitest run --config fork/vitest.config.ts
python3 -m unittest discover -s fork/tests -p 'test_*.py'
python3 fork/ci_build.py
