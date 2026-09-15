#!/usr/bin/env bash
# Run a client against the local dev stack, built with the same deployment profile
# the production targets use. Each client keeps its own implementation; what they
# share is the profile (`<target>.<stage>`), which is what makes the endpoints line
# up with a locally running backend.
#
#   dev/local-client.sh web      [--target self_hosted|cloudflare] [--stage local] [--port 3210]
#   dev/local-client.sh desktop  [--target self_hosted|cloudflare] [--stage local]
#   dev/local-client.sh mobile   [--target self_hosted|cloudflare]
#   dev/local-client.sh stop web
#
# Profiles come from scripts/profiles/render.py, exactly as the server and web
# builds resolve them, so `self_hosted.local` points the client at
# http://127.0.0.1:8100 — the backend `dev/local.sh up` + `dev/selfhost-local.sh up`
# start. Prerequisites per client:
#
#   web      bun 1.3.14 (the version deploy/web/ci.sh pins)
#   desktop  macOS + xcodebuild (compiles only; the fork package never installs or
#            launches an app by design — see desktop/macos/fork/README.md)
#   mobile   Flutter 3.44.5 with its sibling Dart 3.12.2 (app/fork/test.sh asserts the
#            pinned version). The entry finds it without PATH surgery: `OMI_FLUTTER_BIN`
#            if set, else a side-by-side `~/flutter-3.44.5`, else `flutter` on PATH.
#            Install it side-by-side (2.1 GB zip, sha256 442aece6674c4334d46a4f110008a44e835ff53979a8f317333c5e71ccc065b4,
#            from storage.googleapis.com/flutter_infra_release/releases/stable/macos/)
#            and then `cd app && flutter pub get --enforce-lockfile`.

set -euo pipefail

_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$_REPO_ROOT"

CLIENT="${1:-}"
[ -n "$CLIENT" ] || {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
}
shift || true

# `stop` takes the client name positionally; everything else takes options.
if [ "$CLIENT" = "stop" ]; then
  what="${1:-web}"
  pid_file="$_REPO_ROOT/.local/clients/$what.pid"
  if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    kill "$(cat "$pid_file")" 2>/dev/null || true
    rm -f "$pid_file"
    printf '%s client stopped\n' "$what"
  else
    printf '%s client is not running\n' "$what"
  fi
  exit 0
fi

TARGET=self_hosted
STAGE=local
PORT=3210
while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="${2:?--target needs a value}"; shift 2 ;;
    --stage) STAGE="${2:?--stage needs a value}"; shift 2 ;;
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done

STATE_DIR="$_REPO_ROOT/.local/clients"
mkdir -p "$STATE_DIR"
PROFILE="$TARGET.$STAGE"

case "$CLIENT" in
  web)
    command -v bun >/dev/null || { echo "error: bun is required for the web client" >&2; exit 1; }
    version="$(bun --version)"
    [ "$version" = "1.3.14" ] || echo "warning: bun $version, deploy/web/ci.sh pins 1.3.14" >&2
    output="$STATE_DIR/web"
    rm -rf "$output"
    bun deploy/web/build.ts --target "$TARGET" --stage "$STAGE" --output "$output"
    [ -f "$output/artifact/start.js" ] || { echo "error: build produced no artifact/start.js" >&2; exit 1; }
    pid_file="$STATE_DIR/web.pid"
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
      kill "$(cat "$pid_file")" 2>/dev/null || true
      sleep 1
    fi
    (cd "$output/artifact" && PORT="$PORT" nohup bun start.js >"$STATE_DIR/web.log" 2>&1 & echo $! >"$pid_file")
    for _ in $(seq 1 30); do
      curl -sf -m 2 -o /dev/null "http://127.0.0.1:$PORT/conversations" && break
      sleep 1
    done
    printf 'web client (%s): http://127.0.0.1:%s/conversations   (log %s)\n' "$PROFILE" "$PORT" "$STATE_DIR/web.log"
    printf 'stop it with: dev/local-client.sh stop web\n'
    ;;
  desktop)
    [ "$(uname -s)" = "Darwin" ] || { echo "error: the desktop client compiles on macOS only" >&2; exit 1; }
    # Stage + compile both targets, package nothing, launch nothing.
    bash desktop/macos/fork/compile.sh
    printf '\ndesktop client: staged builds for %s are printed above.\n' "self_hosted and cloudflare"
    printf 'The fork package deliberately does not install or launch an app; see\n'
    printf 'desktop/macos/fork/README.md before opening one.\n'
    ;;
  mobile)
    pinned=3.44.5
    # Resolve the pinned SDK the way CI does (subosito/flutter-action at flutter-version
    # 3.44.5), but from a side-by-side install so it never disturbs another Flutter.
    if [ -n "${OMI_FLUTTER_BIN:-}" ] && [ -x "$OMI_FLUTTER_BIN/flutter" ]; then
      PATH="$OMI_FLUTTER_BIN:$PATH"
    elif [ -x "$HOME/flutter-$pinned/bin/flutter" ]; then
      PATH="$HOME/flutter-$pinned/bin:$PATH"
    fi
    export PATH
    current="$(flutter --version 2>/dev/null | head -1 | awk '{print $2}')"
    if [ "$current" != "$pinned" ]; then
      printf 'error: the Flutter client pins %s, this host resolved %s\n' "$pinned" "${current:-none}" >&2
      printf 'Install the pinned SDK side-by-side, then re-run:\n' >&2
      printf '  https://storage.googleapis.com/flutter_infra_release/releases/stable/macos/flutter_macos_arm64_%s-stable.zip\n' "$pinned" >&2
      printf '  ditto -x -k <zip> $HOME/flutter-%s && cd app && flutter pub get --enforce-lockfile\n' "$pinned" >&2
      printf 'Or set OMI_FLUTTER_BIN to the directory holding the pinned flutter binary.\n' >&2
      exit 1
    fi
    test -f app/.dart_tool/package_config.json || {
      printf 'error: app dependencies are not installed for the pinned SDK\n' >&2
      printf 'Run: cd app && flutter pub get --enforce-lockfile\n' >&2
      exit 1
    }
    bash app/fork/test.sh
    ;;
  *)
    printf 'unknown client: %s (expected web | desktop | mobile | stop)\n' "$CLIENT" >&2
    exit 2
    ;;
esac
