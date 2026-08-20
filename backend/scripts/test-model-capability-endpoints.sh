#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
"$PYTHON_BIN" -m pytest -q \
  tests/unit/test_model_capability_endpoints.py \
  tests/unit/test_model_capability_routes.py \
  tests/unit/test_direct_model_fallback.py \
  tests/unit/test_llm_gateway_route_refs.py \
  tests/unit/test_model_neutral_routing.py \
  tests/unit/test_desktop_proactivity.py \
  tests/unit/test_desktop_chat.py \
  tests/unit/test_web_search_tools.py \
  tests/unit/test_desktop_proxy.py \
  tests/unit/test_desktop_realtime.py
