#!/usr/bin/env bash
# Fork-owned entry for the Testcontainers-managed local dev harness.
#
# LIFECYCLE: permanent
#
# Usage:
#   bash scripts/dev-harness/fork_local.sh up        # bring up the containerised stack
#   bash scripts/dev-harness/fork_local.sh status    # what is running, on which ports
#   bash scripts/dev-harness/fork_local.sh down      # stop containers + supervised processes
#   bash scripts/dev-harness/fork_local.sh reset     # down + delete volumes
#   bash scripts/dev-harness/fork_local.sh check     # prerequisite check only
#   bash scripts/dev-harness/fork_local.sh logs <svc>
#
# Replaces upstream host-binary ``make dev-up`` (host redis-server, java +
# firebase-tools, host typesense). Fork ships Testcontainers-managed
# Postgres (firestore_pg backend), MinIO, Redis, Firebase Auth emulator,
# Better Auth TS service, and a Docker-managed Typesense.
#
# Why this is a fork-owned script: the upstream ``scripts/dev-harness/dev-up.sh``
# calls ``python -m dev_harness.cli up`` directly, which forks the host-binary
# prereqs. We cannot redirect that without editing Makefile (upstream-owned, T2
# forbidden). This wrapper invokes ``python -m dev_harness.fork_local`` instead,
# which monkey-patches upstream's ``_start_infrastructure`` /
# ``_harness_service_extra`` / ``prerequisite_report`` at runtime.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

source scripts/dev-harness/_resolve_python.sh
PYTHON_BIN="$(dev_harness_python)"
HARNESS_PYTHONPATH="$(dev_harness_pythonpath "$PYTHON_BIN" scripts/dev-harness)"
dev_harness_require_cli "$PYTHON_BIN" "$HARNESS_PYTHONPATH"

# Resolve any --flag args we may want to add (--no-backend for parity with
# upstream's dev-up.sh flag).
cmd="${1:-check}"
shift || true

PYTHONPATH="$HARNESS_PYTHONPATH" "$PYTHON_BIN" -m dev_harness.fork_local "$cmd" "$@"