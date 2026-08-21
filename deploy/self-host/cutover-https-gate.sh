#!/usr/bin/env bash
# LIFECYCLE: permanent
# Exercise the production public-origin contract behind a disposable local TLS
# edge, or exercise an already-deployed external edge without weakening trust.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE="$ROOT/deploy/self-host/compose.production.yml"
COMPOSE_WRAPPER="$ROOT/deploy/self-host/compose-clean-env.sh"
OVERLAY="$ROOT/deploy/self-host/compose.cutover-acceptance.yml"
SMOKE="$ROOT/deploy/self-host/cutover-live-smoke.py"
OBJECT_EVIDENCE="$ROOT/deploy/self-host/public_object_evidence.py"
AUDIO="$ROOT/backend/testing/release_fixtures/transcription-release-probe.wav"
MANIFEST="$ROOT/backend/testing/release_fixtures/transcription-release-probe.json"
ENV_FILE="${SELF_HOST_ENV:?SELF_HOST_ENV must point to the reviewed production environment file}"
MODE="${1:---local}"

if [[ "$MODE" != --local && "$MODE" != --external ]]; then
  echo "usage: $0 [--local|--external]" >&2
  exit 2
fi

dotenv_value() {
  python3 -c 'import sys; values=dict(line.strip().split("=",1) for line in open(sys.argv[2], encoding="utf-8") if line.strip() and not line.lstrip().startswith("#") and "=" in line); print(values.get(sys.argv[1], ""))' "$1" "$ENV_FILE"
}

AUTH_ORIGIN="$(dotenv_value BETTER_AUTH_TRUSTED_ORIGINS)"
PUBLIC_BACKEND_URL="$(dotenv_value PUBLIC_BACKEND_URL)"
PUBLIC_AUTH_URL="$(dotenv_value PUBLIC_AUTH_URL)"
PUBLIC_MCP_URL="$(dotenv_value PUBLIC_MCP_URL)"
PUBLIC_OBJECTS_URL="$(dotenv_value PUBLIC_OBJECTS_URL)"
DIARIZATION_AUDIO="$(dotenv_value MLX_MOSS_DIARIZE_ACCEPTANCE_WAV_HOST_PATH)"
if [[ "$DIARIZATION_AUDIO" != /* || ! -f "$DIARIZATION_AUDIO" ]]; then
  echo "ERROR: MLX_MOSS_DIARIZE_ACCEPTANCE_WAV_HOST_PATH must be an existing absolute WAV file" >&2
  exit 1
fi
CERT_DIR=''
CA_FILE='/etc/ssl/certs/ca-certificates.crt'
LIVE_EGRESS_EVIDENCE_JSON='{"enforcement":"not_enforced_by_compose","sentinel_targets_denied":[],"workloads":[],"operator_policy_artifact_sha256":null,"scope":"compose_has_no_application_egress_policy"}'
compose() {
  bash "$COMPOSE_WRAPPER" "$ENV_FILE" "$COMPOSE" "$@"
}

profile_compose() {
  if [[ "$MODE" == --local ]]; then
    CUTOVER_HTTPS_PORT="${CUTOVER_HTTPS_PORT:-18443}" \
    CUTOVER_TLS_CERT_PATH="$CERT_DIR/server.crt" \
    CUTOVER_TLS_KEY_PATH="$CERT_DIR/server.key" \
      bash "$COMPOSE_WRAPPER" "$ENV_FILE" "$COMPOSE" --file "$OVERLAY" "$@"
  else
    compose "$@"
  fi
}

cleanup() {
  if [[ -n "$CERT_DIR" ]]; then
    profile_compose stop https-proxy realtime-relay-fixture >/dev/null 2>&1 || true
    profile_compose rm -f https-proxy realtime-relay-fixture >/dev/null 2>&1 || true
    # The local overlay replaces only the backend relay target. Restore the
    # exact reviewed production service before runtime attribution is collected.
    compose up --detach --wait --no-deps backend >/dev/null 2>&1 || true
    rm -rf "$CERT_DIR"
  fi
}
trap cleanup EXIT

if [[ "$MODE" == --local ]]; then
  CUTOVER_HTTPS_PORT="${CUTOVER_HTTPS_PORT:-18443}"
  # The smoke runs on the Compose network, so the reviewed public origins use
  # the proxy's real TLS listener (:443). CUTOVER_HTTPS_PORT is only an
  # optional loopback host publication for operator diagnostics.
  expected_backend='https://api.omi.test'
  expected_auth='https://auth.omi.test'
  expected_mcp='https://mcp.omi.test'
  expected_objects='https://objects.omi.test'
  if [[ "$PUBLIC_BACKEND_URL" != "$expected_backend" || "$PUBLIC_AUTH_URL" != "$expected_auth" || "$PUBLIC_MCP_URL" != "$expected_mcp" || "$PUBLIC_OBJECTS_URL" != "$expected_objects" ]]; then
    echo "ERROR: --local requires the four public URLs to use the acceptance HTTPS .omi.test origins without a port" >&2
    exit 1
  fi
  CERT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/omi-cutover-tls.XXXXXX")"
  openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -subj '/CN=Omi self-host cutover acceptance' \
    -addext 'subjectAltName=DNS:api.omi.test,DNS:auth.omi.test,DNS:mcp.omi.test,DNS:objects.omi.test' \
    -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.crt" >/dev/null 2>&1
  CA_FILE='/tmp/omi-cutover-ca.crt'
  profile_compose up --detach --wait realtime-relay-fixture backend https-proxy
else
  for value in "$PUBLIC_BACKEND_URL" "$PUBLIC_AUTH_URL" "$PUBLIC_MCP_URL" "$PUBLIC_OBJECTS_URL"; do
    case "$value" in
      https://*.localhost*|https://localhost*|https://*.test*|https://127.*|https://\[*|https://*.invalid*)
        echo "ERROR: --external refuses local or reserved public origin $value" >&2
        exit 1
        ;;
    esac
  done

  # Sentinel probes are behavioral corroboration for an operator-supplied
  # network-policy artifact; they are not a claim that every public address is
  # unreachable. SearXNG is intentionally excluded because it owns the reviewed
  # Wikipedia egress allowlist.
  if [[ -z "${SELF_HOST_EGRESS_POLICY_ARTIFACT:-}" ]]; then
    echo "ERROR: --external requires SELF_HOST_EGRESS_POLICY_ARTIFACT" >&2
    exit 1
  fi
  EGRESS_POLICY_ARTIFACT="$SELF_HOST_EGRESS_POLICY_ARTIFACT"
  if [[ ! -s "$EGRESS_POLICY_ARTIFACT" ]]; then
    echo "ERROR: external egress policy artifact is missing or empty: $EGRESS_POLICY_ARTIFACT" >&2
    exit 1
  fi
  EGRESS_POLICY_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$EGRESS_POLICY_ARTIFACT")"
  SENTINEL_TARGETS_JSON='["api.openai.com","generativelanguage.googleapis.com","api.anthropic.com","api.omi.me","1.1.1.1"]'
  WORKLOAD_STATUS_JSON="$(profile_compose ps --format json backend queue-worker auth-server)"
  if ! python3 -c 'import json,sys; rows=[json.loads(line) for line in sys.argv[1].splitlines() if line.strip()]; expected={"backend","queue-worker","auth-server"}; healthy={row.get("Service") for row in rows if row.get("State")=="running" and row.get("Health")=="healthy"}; raise SystemExit(0 if healthy==expected else 1)' "$WORKLOAD_STATUS_JSON"; then
    echo "ERROR: backend, queue-worker, and auth-server must be the running healthy workloads before external egress probes" >&2
    exit 1
  fi
  PYTHON_DENIAL_PROBE='import json,socket,sys
targets=json.loads(sys.argv[1])
for target in targets:
    try:
        connection=socket.create_connection((target,443),3)
    except OSError:
        continue
    connection.close()
    print(target, file=sys.stderr)
    raise SystemExit(1)'
  for service in backend queue-worker; do
    if profile_compose exec -T "$service" python -c "$PYTHON_DENIAL_PROBE" "$SENTINEL_TARGETS_JSON"; then
      :
    else
      echo "ERROR: $service can open a reviewed vendor/arbitrary sentinel socket; enforce the supplied production egress policy" >&2
      exit 1
    fi
  done
  NODE_DENIAL_PROBE='const net=require("net"); const targets=JSON.parse(process.argv[1]); Promise.all(targets.map((host)=>new Promise((resolve)=>{const s=net.createConnection({host,port:443}); const t=setTimeout(()=>{s.destroy();resolve(null)},3000); s.on("error",()=>{clearTimeout(t);resolve(null)}); s.on("connect",()=>{clearTimeout(t);s.destroy();resolve(host)})}))).then((opened)=>{const host=opened.find(Boolean); if(host){console.error(host);process.exit(1)} process.exit(0)})'
  if ! profile_compose exec -T auth-server node -e "$NODE_DENIAL_PROBE" "$SENTINEL_TARGETS_JSON"; then
    echo "ERROR: auth-server can open a reviewed vendor/arbitrary sentinel socket; enforce the supplied production egress policy" >&2
    exit 1
  fi
  LIVE_EGRESS_EVIDENCE_JSON="$(python3 -c 'import json,sys; print(json.dumps({"enforcement":"sentinel_targets_denied_with_operator_policy","sentinel_targets_denied":json.loads(sys.argv[1]),"workloads":["backend","queue-worker","auth-server"],"operator_policy_artifact_sha256":sys.argv[2],"scope":"sentinel_targets_only"}, separators=(",", ":")))' "$SENTINEL_TARGETS_JSON" "$EGRESS_POLICY_SHA256")"
fi

CONFIGURED_SEARXNG_SECRET="$(dotenv_value SEARXNG_SECRET)"
if [[ -z "$CONFIGURED_SEARXNG_SECRET" ]]; then
  echo "ERROR: SEARXNG_SECRET is missing from the reviewed environment file" >&2
  exit 1
fi
CONFIGURED_SEARXNG_SECRET_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$CONFIGURED_SEARXNG_SECRET")"
SEARXNG_EFFECTIVE_SETTINGS_JSON="$(profile_compose exec -T searxng /usr/local/searxng/.venv/bin/python -c 'import hashlib,json; from searx import settings; value=str(settings.get("server",{}).get("secret_key", "")); evidence={"effective_secret_nonempty":bool(value),"effective_secret_not_known_default":value not in {"", "ultrasecretkey"},"effective_secret_sha256":hashlib.sha256(value.encode()).hexdigest()}; print(json.dumps(evidence,separators=(",", ":")))')"
if ! SEARXNG_SETTINGS_EVIDENCE_JSON="$(python3 -c 'import hmac,json,sys; value=json.loads(sys.argv[1]); matches=hmac.compare_digest(str(value.get("effective_secret_sha256") or ""), sys.argv[2]); safe=value.get("effective_secret_nonempty") is True and value.get("effective_secret_not_known_default") is True and matches; print(json.dumps({"effective_secret_nonempty":value.get("effective_secret_nonempty") is True,"effective_secret_not_known_default":value.get("effective_secret_not_known_default") is True,"effective_secret_matches_configured":matches}, separators=(",", ":"))); raise SystemExit(0 if safe else 1)' "$SEARXNG_EFFECTIVE_SETTINGS_JSON" "$CONFIGURED_SEARXNG_SECRET_SHA256")"; then
  echo "ERROR: SearXNG effective settings did not apply a non-default runtime secret" >&2
  exit 1
fi

RUN_ARGS=(
  run --rm --no-deps -T
  --volume "$SMOKE:/tmp/cutover-live-smoke.py:ro"
  --volume "$OBJECT_EVIDENCE:/tmp/public_object_evidence.py:ro"
  --volume "$AUDIO:/tmp/transcription-release-probe.wav:ro"
  --volume "$DIARIZATION_AUDIO:/tmp/mlx-moss-diarization-acceptance.wav:ro"
  --volume "$MANIFEST:/tmp/transcription-release-probe.json:ro"
  --env "SELF_HOST_AUTH_ORIGIN=${AUTH_ORIGIN%%,*}"
  --env "PUBLIC_BACKEND_URL=$PUBLIC_BACKEND_URL"
  --env "PUBLIC_AUTH_URL=$PUBLIC_AUTH_URL"
  --env "PUBLIC_MCP_URL=$PUBLIC_MCP_URL"
  --env "PUBLIC_OBJECTS_URL=$PUBLIC_OBJECTS_URL"
  --env "SELF_HOST_CUTOVER_CA_FILE=$CA_FILE"
  --env SELF_HOST_CAPTURE_WAV=/tmp/transcription-release-probe.wav
  --env SELF_HOST_DIARIZATION_WAV=/tmp/mlx-moss-diarization-acceptance.wav
  --env SELF_HOST_CAPTURE_MANIFEST=/tmp/transcription-release-probe.json
  --env "SELF_HOST_ACCEPTANCE_ALLOW_CONTROL_SEED=${SELF_HOST_ACCEPTANCE_ALLOW_CONTROL_SEED:-false}"
  --env "SELF_HOST_LIVE_EGRESS_EVIDENCE_JSON=$LIVE_EGRESS_EVIDENCE_JSON"
  --env "SELF_HOST_SEARXNG_SETTINGS_EVIDENCE_JSON=$SEARXNG_SETTINGS_EVIDENCE_JSON"
)
if [[ "$MODE" == --local ]]; then
  RUN_ARGS+=(--volume "$CERT_DIR/server.crt:$CA_FILE:ro")
fi

profile_compose "${RUN_ARGS[@]}" backend python /tmp/cutover-live-smoke.py
