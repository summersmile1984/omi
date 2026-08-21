#!/usr/bin/env bash
# LIFECYCLE: permanent
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
backend_root="$(cd "$script_dir/.." && pwd)"
python_bin="${PYTHON:-$backend_root/.venv/bin/python}"

if [[ ! -x "$python_bin" ]]; then
  python_bin=python3
fi

cd "$backend_root"
PYTHONDONTWRITEBYTECODE=1 "$python_bin" scripts/export_authoritative_vectors.py --help >/dev/null
PYTHONDONTWRITEBYTECODE=1 "$python_bin" -m pytest -q tests/unit/test_authoritative_vector_export.py
