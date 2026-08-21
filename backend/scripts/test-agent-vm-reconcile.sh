#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="${PYTHON:-$BACKEND_ROOT/.venv/bin/python}"

if [[ ! -x "$PY" ]]; then
  PY=python3
fi

cd "$BACKEND_ROOT"
PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q tests/unit/test_agent_vm_reconcile.py
